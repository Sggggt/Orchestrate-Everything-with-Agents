"""Atomic capacity admission and durable leases; the Runtime owns scheduling."""

import asyncio
import json
import heapq
from contextlib import asynccontextmanager

from .errors import RuntimeFault, require
from .models import (QuantityRequirement, ResourceCapacity, ResourceLeaseSpec,
                     ResourceRequirement, content_hash, json_dump, new_id, now_ms)
from .store import emit, one, rows


class ResourceCoordinator:
    def __init__(self, runtime=None):
        self.rt = runtime
        self.holders: dict[str, dict[str, str]] = {}
        self.isolated: dict[str, str] = {}
        self.capacities: dict[str, ResourceCapacity] = {}
        self.quantities: dict[str, dict[str, int]] = {}
        self.pending: dict[str, ResourceLeaseSpec] = {}
        self.expirations: dict[str, int] = {}
        self.deadlines = []
        self.isolated_leases = set()
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.phase_waiters = {}
        self.owner_keys: dict[str, tuple[set[str], set[str]]] = {}
        self.recovery_slots = asyncio.Semaphore(2)
        self.recovery_tasks = {}
        self.run_leases = {}
        self.lease_runs = {}

    def define(self, key, limit, *, unit="operation", reserved=0):
        current = self.capacities.get(key)
        if current and (current.limit, current.unit, current.reserved) == (limit, unit, reserved):
            return
        self.capacities[key] = ResourceCapacity(key=key, limit=limit, unit=unit, reserved=reserved)

    def defaults(self, key, limit, *, unit="operation"):
        if key not in self.capacities:
            self.define(key, limit, unit=unit)

    def retire_connection_capacity(self, key):
        if key.startswith("mcp:connection:") and key not in self.quantities and not any(key in keys for keys in self.phase_waiters.values()):
            self.capacities.pop(key, None)

    def reasons(self, owner, requirements=(), quantities=()):
        reasons = []
        for item in requirements:
            if any(held != owner and (mode == "EXCLUSIVE" or item.mode == "EXCLUSIVE")
                   for held, mode in self.holders.get(item.key, {}).items()):
                reasons.append({"key": item.key, "reason": "RESOURCE_BUSY"})
        for item in quantities:
            config = self.capacities.get(item.key)
            require(config is not None, "RESOURCE_UNDEFINED", "Quantity resource is not configured", key=item.key)
            used = sum(count for held, count in self.quantities.get(item.key, {}).items() if held != owner)
            if used + item.quantity > config.limit - config.reserved:
                reasons.append({"key": item.key, "reason": "CAPACITY", "unit": config.unit,
                                "requested": item.quantity, "used": used,
                                "available": max(0, config.limit - config.reserved - used)})
        return reasons

    def can_acquire(self, owner, requirements=(), quantities=()):
        return not self.reasons(owner, requirements, quantities)

    def acquire(self, owner, requirements=(), quantities=()):
        if not self.can_acquire(owner, requirements, quantities):
            return False
        self.restore(owner, requirements, quantities)
        return True

    def restore(self, owner, requirements=(), quantities=()):
        # Account for actual occupancy even if capacity was reduced while offline.
        if not requirements and not quantities:
            return
        keys, counts = self.owner_keys.setdefault(owner, (set(), set()))
        for item in requirements:
            self.holders.setdefault(item.key, {})[owner] = item.mode
            keys.add(item.key)
        for item in quantities:
            self.quantities.setdefault(item.key, {})[owner] = item.quantity
            counts.add(item.key)

    def release(self, owner):
        if owner in self.isolated:
            return
        for collection, keys in zip((self.holders, self.quantities), self.owner_keys.pop(owner, ((), ()))):
            for key in keys:
                collection[key].pop(owner, None)
                if not collection[key]:
                    del collection[key]
        self.changed.set()
        if self.rt:
            self.rt.wake.set()

    def isolate(self, owner, reason):
        if owner in self.owner_keys:
            self.isolated[owner] = reason

    def resolve(self, owner):
        self.isolated.pop(owner, None)
        self.release(owner)

    def release_execution(self, owner):
        """Release only machine-confirmed local execution claims; external claims stay held."""
        indexed = self.owner_keys.get(owner)
        if indexed:
            keys, counts = indexed
            for key in tuple(counts):
                if key.startswith("execution:"):
                    self.quantities[key].pop(owner, None)
                    if not self.quantities[key]:
                        del self.quantities[key]
                    counts.remove(key)
            if not keys and not counts:
                self.owner_keys.pop(owner)
                self.isolated.pop(owner, None)
        self.rt.uncertain_slots.discard(owner)
        self.rt._uncertain_owners.pop(owner, None)
        self.changed.set()
        self.rt.notify()

    async def execution_stopped(self, run_id, evidence):
        def confirm(db):
            run = one(db, "SELECT state,adapter_snapshot_json FROM runs WHERE id=?", (run_id,))
            if run is None or run.state != "UNCERTAIN":
                return False
            descriptor = json.loads(run.adapter_snapshot_json)
            if descriptor["tool_mediation"] != "full" or descriptor["nested_execution"] == "unmanaged":
                return False
            result = {"state": "EXECUTION_RELEASED", "generation": run_id, "evidence": evidence}
            stamp = now_ms()
            db.execute("INSERT OR IGNORE INTO resource_recoveries VALUES(?,?,?,?,?,?,?)",
                       (new_id("recovery"), run_id, "execution-stop", run_id, json_dump(result), stamp, stamp))
            if db.changes():
                emit(db, "resource.execution_released", run_id=run_id, payload={"evidence": evidence})
            return True
        if await self.rt.store.call(confirm, transaction=True):
            self.release_execution(run_id)
            await self.release_run_leases(run_id, evidence)
            return True
        return False

    def schedule_execution_recovery(self, active):
        run_id = active.request.run_id
        if run_id in self.recovery_tasks or self.rt.store.closed:
            return
        task = asyncio.create_task(self.observe_execution(active), name="execution-recovery:" + run_id)
        self.recovery_tasks[run_id] = task
        def finished(done):
            self.recovery_tasks.pop(run_id, None)
            if not done.cancelled() and done.exception() is not None:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "Execution resource recovery failed; occupancy remains held", "exception": done.exception()})
        task.add_done_callback(finished)

    async def observe_execution(self, active):
        async with self.recovery_slots:
            if not active.worker.done():
                return False
            probe = await self.rt.probe(active.adapter, {"run_id": active.request.run_id})
            if probe.confirmed_stopped:
                await self.release_run_leases(active.request.run_id,
                    {"kind": "adapter_stop_probe", "state": probe.state, "evidence": probe.evidence})
                return await self.execution_stopped(active.request.run_id,
                    {"kind": "adapter_stop_probe", "state": probe.state, "evidence": probe.evidence})
            return False

    async def drain(self):
        if self.recovery_tasks:
            await asyncio.gather(*tuple(self.recovery_tasks.values()), return_exceptions=True)

    def execution_claims(self, agent_id, agent_limit, batch_id=None, batch_limit=None):
        config = self.rt.config
        self.define("execution:global", config.max_concurrency, unit="run", reserved=config.reserved_external_slots)
        self.define("execution:agent:" + agent_id, agent_limit, unit="run")
        keys = ["execution:global", "execution:agent:" + agent_id]
        if batch_id:
            self.define("execution:batch:" + batch_id, batch_limit, unit="run")
            keys.append("execution:batch:" + batch_id)
        return tuple(QuantityRequirement(key=key) for key in keys)

    async def open(self):
        configs, leases = await self.rt.store.call(lambda db: (
            rows(db, "SELECT * FROM resource_capacities"),
            rows(db, "SELECT * FROM resource_leases WHERE state!='RELEASED' ORDER BY created_at,rowid")))
        for row in configs:
            config = ResourceCapacity.model_validate_json(row.config_json)
            self.capacities[config.key] = config
        for row in leases:
            spec = ResourceLeaseSpec.model_validate_json(row.spec_json)
            if spec.owner_run_id:
                self.run_leases.setdefault(spec.owner_run_id, set()).add(row.id)
                self.lease_runs[row.id] = spec.owner_run_id
            if row.state == "QUEUED":
                self.pending[row.id] = spec
            else:
                self.restore(row.id, spec.resources, spec.quantities)
                self.isolate(row.id, "Lease owner must confirm execution stopped after host recovery")
                self.isolated_leases.add(row.id)
        if any(row.state == "GRANTED" for row in leases):
            def recover(db):
                for row in leases:
                    if row.state == "GRANTED":
                        db.execute("UPDATE resource_leases SET state='ISOLATED' WHERE id=?", (row.id,))
                        emit(db, "resource.isolated", payload={"lease_id": row.id, "reason": "HOST_RECOVERY"})
            await self.rt.store.call(recover, transaction=True)

    async def configure(self, config):
        config = ResourceCapacity.model_validate(config)
        require(not config.key.startswith(("execution:", "mcp:")), "INVALID_REQUEST", "Execution and MCP capacities follow their owning configuration services")
        async with self.lock:
            await self.rt.store.call(lambda db: db.execute(
                "INSERT INTO resource_capacities(key,config_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET config_json=excluded.config_json",
                (config.key, json_dump(config))), transaction=True)
            self.capacities[config.key] = config
            self.changed.set()
            self.rt.notify()
        return config

    async def preflight(self, quantities=(), resources=()):
        quantities = tuple(QuantityRequirement.model_validate(item) for item in quantities)
        resources = tuple(ResourceRequirement.model_validate(item) for item in resources)
        require(len(quantities) <= 32 and len(resources) <= 32, "INVALID_REQUEST", "Too many resource requirements")
        require(len({item.key for item in quantities}) == len(quantities), "INVALID_REQUEST", "Quantity keys must be unique")
        reasons = self.reasons("", resources, quantities)
        return {"admissible": not reasons, "reasons": reasons, "reservation": False}

    async def request(self, spec):
        self.rt.admit()
        spec = ResourceLeaseSpec.model_validate(spec)
        digest = content_hash(spec)
        async with self.lock:
            def insert(db):
                prior = one(db, "SELECT * FROM resource_leases WHERE owner=? AND scope=? AND idempotency_key=?",
                            (spec.owner, spec.scope, spec.idempotency_key))
                if prior:
                    require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Lease key already belongs to another request")
                    return prior.id, True
                if spec.owner_run_id:
                    owner = one(db, "SELECT state,adapter_snapshot_json FROM runs WHERE id=?", (spec.owner_run_id,))
                    require(owner is not None and owner.state == "RUNNING", "INVALID_OWNER", "A new run-bound lease requires a running owner")
                    descriptor = json.loads(owner.adapter_snapshot_json)
                    require(descriptor["tool_mediation"] == "full" and descriptor["nested_execution"] != "unmanaged",
                            "UNSUPPORTED_CAPABILITY", "A run-bound lease requires fully mediated execution")
                require(len(self.pending) + len(self.expirations) + len(self.isolated_leases) < self.rt.config.max_queued_tasks,
                        "QUEUE_FULL", "Resource lease queue has reached the configured pending-work limit")
                identity = new_id("lease")
                db.execute("INSERT INTO resource_leases(id,owner,scope,idempotency_key,request_hash,spec_json,state,created_at) VALUES(?,?,?,?,?,?,'QUEUED',?)",
                           (identity, spec.owner, spec.scope, spec.idempotency_key, digest, json_dump(spec), now_ms()))
                emit(db, "resource.requested", payload={"lease_id": identity, "owner": spec.owner, "scope": spec.scope, "phase": spec.phase})
                return identity, False
            self.reasons("", spec.resources, spec.quantities)
            if spec.owner_run_id:
                self.run_leases.setdefault(spec.owner_run_id, set())
            try:
                identity, reused = await self.rt.store.call(insert, transaction=True)
            except BaseException:
                if spec.owner_run_id and not self.run_leases.get(spec.owner_run_id):
                    self.run_leases.pop(spec.owner_run_id, None)
                raise
            if not reused:
                self.pending[identity] = spec
                if spec.owner_run_id:
                    self.run_leases.setdefault(spec.owner_run_id, set()).add(identity)
                    self.lease_runs[identity] = spec.owner_run_id
            if spec.owner_run_id and not self.run_leases[spec.owner_run_id]:
                self.run_leases.pop(spec.owner_run_id)
            self.rt.notify()
        return {**await self.get(identity), "reused": reused}

    async def get(self, lease_id):
        row = await self.rt.store.call(lambda db: one(db, "SELECT * FROM resource_leases WHERE id=?", (lease_id,)))
        require(row is not None, "NOT_FOUND", "Resource lease does not exist")
        spec = ResourceLeaseSpec.model_validate_json(row.spec_json)
        return {"id": row.id, "state": row.state, "spec": spec.model_dump(mode="json"),
                "created_at": row.created_at, "granted_at": row.granted_at,
                "expires_at": row.expires_at, "ended_at": row.ended_at, "evidence": row.evidence,
                "queue_reasons": self.reasons(row.id, spec.resources, spec.quantities) if row.state == "QUEUED" else []}

    async def release_lease(self, lease_id, *, confirmed_stopped=False, evidence=""):
        async with self.lock:
            def commit(db):
                row = one(db, "SELECT * FROM resource_leases WHERE id=?", (lease_id,))
                require(row is not None, "NOT_FOUND", "Resource lease does not exist")
                if row.state == "RELEASED":
                    return
                require(row.state == "QUEUED" or (confirmed_stopped and bool(evidence.strip())),
                        "RECONCILIATION_REQUIRED", "Confirm the owner stopped using the lease and provide evidence")
                require(len(evidence) <= 4096, "INVALID_REQUEST", "Evidence exceeds 4096 characters")
                db.execute("UPDATE resource_leases SET state='RELEASED',ended_at=?,evidence=? WHERE id=?",
                           (now_ms(), evidence, lease_id))
                emit(db, "resource.released", payload={"lease_id": lease_id, "confirmed_stopped": confirmed_stopped})
            await self.rt.store.call(commit, transaction=True)
            self.forget_lease(lease_id)
        return await self.get(lease_id)

    def forget_lease(self, lease_id):
        self.pending.pop(lease_id, None)
        self.expirations.pop(lease_id, None)
        self.isolated_leases.discard(lease_id)
        run_id = self.lease_runs.pop(lease_id, None)
        if run_id:
            self.run_leases[run_id].discard(lease_id)
            if not self.run_leases[run_id]:
                self.run_leases.pop(run_id)
        if len(self.deadlines) > max(64, len(self.expirations) * 2):
            self.deadlines = [(expiry, identity) for identity, expiry in self.expirations.items()]
            heapq.heapify(self.deadlines)
        self.resolve(lease_id)

    async def release_run_leases(self, run_id, evidence):
        if run_id not in self.run_leases:
            return
        async with self.lock:
            def stop(db):
                found = rows(db, "SELECT id FROM resource_leases WHERE owner=? AND state!='RELEASED' "
                             "AND json_extract(spec_json,'$.owner_run_id')=?", (run_id, run_id))
                if found:
                    stamp = now_ms()
                    db.execute("INSERT OR IGNORE INTO resource_recoveries VALUES(?,?,?,?,?,?,?)",
                               (new_id("recovery"), run_id, "execution-stop", run_id,
                                json_dump({"state": "EXECUTION_RELEASED", "generation": run_id, "evidence": evidence}), stamp, stamp))
                for lease in found:
                    db.execute("UPDATE resource_leases SET state='RELEASED',ended_at=?,evidence=? WHERE id=?",
                               (now_ms(), json_dump(evidence), lease.id))
                    emit(db, "resource.released", run_id=run_id,
                         payload={"lease_id": lease.id, "confirmed_stopped": True, "automatic": True})
                return found
            found = await self.rt.store.call(stop, transaction=True)
            for lease in found:
                self.forget_lease(lease.id)
            if not self.run_leases.get(run_id):
                self.run_leases.pop(run_id, None)

    async def inspect_execution_stop(self, run_id):
        active = self.rt.active.get(run_id)
        if active:
            return await self.observe_execution(active)
        run = await self.rt.store.call(lambda db: one(db,
            "SELECT r.*,t.spec_json,x.id AS execution_released FROM runs r JOIN tasks t ON t.id=r.task_id "
            "LEFT JOIN resource_recoveries x ON x.owner=r.id AND x.idempotency_key='execution-stop' WHERE r.id=?", (run_id,)))
        if run is None:
            return False
        async with self.recovery_slots:
            adapter = self.rt.adapters.get(json.loads(run.spec_json)["agent_id"])
            probe = await self.rt.probe_recorded_run(run, adapter)
            if not probe.confirmed_stopped:
                return False
            evidence = {"kind": "recorded_run_probe", "state": probe.state, "evidence": probe.evidence}
            await self.release_run_leases(run_id, evidence)
            await self.execution_stopped(run_id, evidence)
            return "execution:global" not in self.owner_keys.get(run_id, (set(), set()))[1]

    async def recover_committed_run_leases(self):
        for run_id in tuple(self.run_leases):
            row = await self.rt.store.call(lambda db, identity=run_id: one(db, "SELECT state FROM runs WHERE id=?", (identity,)))
            if row is not None and row.state not in {"RUNNING", "UNCERTAIN"}:
                await self.inspect_execution_stop(run_id)

    async def tick(self):
        if not self.pending and not self.expirations:
            return
        async with self.lock:
            stamp = now_ms()
            while self.deadlines and self.deadlines[0][0] <= stamp:
                expiry, identity = heapq.heappop(self.deadlines)
                if self.expirations.get(identity) != expiry:
                    continue
                def expire(db, identity=identity):
                    db.execute("UPDATE resource_leases SET state='ISOLATED' WHERE id=? AND state='GRANTED'", (identity,))
                    emit(db, "resource.isolated", payload={"lease_id": identity, "reason": "LEASE_EXPIRED"})
                await self.rt.store.call(expire, transaction=True)
                self.expirations.pop(identity)
                self.isolated_leases.add(identity)
                self.isolate(identity, "Lease expired; stopping the owner has not been confirmed")
            blocked = set()
            for identity, spec in tuple(self.pending.items()):
                keys = {item.key for item in (*spec.resources, *spec.quantities)}
                if keys & blocked or not self.acquire(identity, spec.resources, spec.quantities):
                    blocked.update(keys)
                    continue
                expiry = now_ms() + int(spec.ttl_seconds * 1000)
                def grant(db, identity=identity, expiry=expiry):
                    db.execute("UPDATE resource_leases SET state='GRANTED',granted_at=?,expires_at=? WHERE id=?",
                               (now_ms(), expiry, identity))
                    emit(db, "resource.granted", payload={"lease_id": identity})
                try:
                    await self.rt.store.call(grant, transaction=True)
                except BaseException:
                    self.release(identity)
                    raise
                self.pending.pop(identity)
                self.expirations[identity] = expiry
                heapq.heappush(self.deadlines, (expiry, identity))
            self.changed.set()

    @asynccontextmanager
    async def phase(self, owner, quantities, *, timeout):
        """A bounded phase wait retains its caller's actual execution slot."""
        keys = {item.key for item in quantities}
        self.phase_waiters[owner] = keys
        acquired = False
        try:
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        self.changed.clear()
                        older = set()
                        for waiting, waiting_keys in self.phase_waiters.items():
                            if waiting == owner:
                                break
                            older.update(waiting_keys)
                        if not keys & older and self.acquire(owner, (), quantities):
                            acquired = True
                            self.phase_waiters.pop(owner)
                            self.changed.set()
                            break
                        await self.changed.wait()
            except TimeoutError:
                raise RuntimeFault("RESOURCE_BUSY", "Phase capacity did not become available within the bounded wait",
                                   retryable=True, details={"stage": "capacity", "dispatched": False}) from None
            yield
        finally:
            self.phase_waiters.pop(owner, None)
            if acquired:
                self.release(owner)
            self.changed.set()

    async def inspect(self):
        return {"resources": [{"key": key, "holders": [
            {"owner": owner, "mode": mode, "isolated": owner in self.isolated, "reason": self.isolated.get(owner)}
            for owner, mode in holders.items()]} for key, holders in sorted(self.holders.items())],
            "capacities": [{**config.model_dump(mode="json"),
                "used": sum(self.quantities.get(key, {}).values()),
                "holders": [{"owner": owner, "quantity": quantity, "isolated": owner in self.isolated}
                            for owner, quantity in self.quantities.get(key, {}).items()]}
                for key, config in sorted(self.capacities.items())],
            "queued_leases": list(self.pending), "phase_waiters": list(self.phase_waiters)}

    async def status(self, scope=None, cursor=None, limit=25):
        scope = {} if scope is None else scope
        require(isinstance(scope, dict) and set(scope) <= {"owner_id", "resource_key", "session_id", "project_id"}
                and all(isinstance(value, str) and value for value in scope.values()),
                "INVALID_REQUEST", "Choose owner_id, resource_key, session_id or project_id scope")
        require(type(limit) is int and 1 <= limit <= 100 and (cursor is None or isinstance(cursor, str)),
                "INVALID_REQUEST", "Use a string cursor and limit 1..100")
        owners = sorted(owner for owner, (keys, counts) in self.owner_keys.items()
                        if (not cursor or owner > cursor) and (not scope.get("owner_id") or owner == scope["owner_id"])
                        and (not scope.get("resource_key") or scope["resource_key"] in keys | counts))
        workload = None
        if scope.get("session_id") or scope.get("project_id"):
            live_owners = tuple(self.owner_keys)
            bound_runs = tuple(self.lease_runs.values())
            def scoped(db):
                conditions, params = [], []
                for key in ("session_id", "project_id"):
                    if key in scope:
                        conditions.append(("t.session_id" if key == "session_id" else "s.project_id") + "=?")
                        params.append(scope[key])
                where = " AND ".join(conditions)
                calls = rows(db, "SELECT c.id,x.connection_owner FROM mcp_calls c JOIN mcp_call_resources x ON x.call_id=c.id "
                             "JOIN runs r ON r.id=c.run_id JOIN tasks t ON t.id=r.task_id JOIN sessions s ON s.id=t.session_id "
                             "WHERE x.call_id IN (" + ",".join("?" for _ in live_owners) + ") AND x.state!='RELEASED' AND " + where,
                             (*live_owners, *params)) if live_owners else []
                candidates = tuple({owner.partition(":")[0] for owner in live_owners} | set(bound_runs))
                runs = rows(db, "SELECT r.id,h.agent_id FROM runs r JOIN tasks t ON t.id=r.task_id "
                            "JOIN threads h ON h.id=t.thread_id JOIN sessions s ON s.id=t.session_id "
                            "WHERE r.id IN (" + ",".join("?" for _ in candidates) + ") AND " + where,
                            (*candidates, *params)) if candidates else []
                flow_where = where.replace("t.session_id", "w.session_id")
                workflows = one(db, "SELECT COUNT(*) AS count FROM workflow_runs w JOIN sessions s ON s.id=w.session_id "
                                "WHERE w.ended_at IS NULL AND " + flow_where, params).count
                eligible = {run.id for run in runs} | {call.id for call in calls} | {call.connection_owner for call in calls}
                counts = {}
                for run in runs:
                    if "execution:global" in self.owner_keys.get(run.id, (set(), set()))[1]:
                        counts[run.agent_id] = counts.get(run.agent_id, 0) + 1
                return eligible, {"unfinished_workflows": workflows, "executing_instances": counts,
                                  "recommended_workflows": 4, "recommended_instances_per_agent": 6}
            eligible, workload = await self.rt.store.call(scoped)
            owners = [owner for owner in owners if owner in eligible or self.lease_runs.get(owner) in eligible
                      or any(owner.startswith(run + ":") for run in eligible)]
        owners = [owner for owner in owners if owner in self.owner_keys]
        items = []
        for owner in owners[:limit]:
            keys, counts = self.owner_keys[owner]
            items.append({"owner_id": owner, "isolated": owner in self.isolated, "reason": self.isolated.get(owner),
                          "resources": [{"key": key, "mode": self.holders[key][owner]} for key in sorted(keys)],
                          "quantities": [{"key": key, "quantity": self.quantities[key][owner],
                              "limit": self.capacities[key].limit, "used": sum(self.quantities[key].values()),
                              "available": max(0, self.capacities[key].limit - self.capacities[key].reserved - sum(self.quantities[key].values()))}
                             for key in sorted(counts)]})
        return {"items": items, "next_cursor": owners[limit - 1] if len(owners) > limit else None,
                "workload": workload, "recovery_in_progress": len(set(self.rt.mcp.capacity.watching) | set(self.rt.mcp.capacity.recoveries)) + len(self.recovery_tasks)}

    async def recover(self, owner_id, idempotency_key):
        self.rt.admit()
        require(isinstance(owner_id, str) and 0 < len(owner_id) <= 256 and isinstance(idempotency_key, str)
                and 0 < len(idempotency_key) <= 256, "INVALID_REQUEST", "Supply a bounded owner_id and stable idempotency_key")
        def begin(db):
            prior = one(db, "SELECT * FROM resource_recoveries WHERE owner=? AND idempotency_key=?", (owner_id, idempotency_key))
            if prior:
                return prior.id, prior.generation, True
            call = one(db, "SELECT connection_owner FROM mcp_call_resources WHERE call_id=?", (owner_id,))
            lease = None if call else one(db, "SELECT id FROM resource_leases WHERE id=?", (owner_id,))
            run = None if call or lease else one(db, "SELECT id FROM runs WHERE id=?", (owner_id,))
            require(call or lease or run, "NOT_FOUND", "Choose a managed call, run or lease owner")
            generation = call.connection_owner if call else owner_id
            identity, stamp = new_id("recovery"), now_ms()
            db.execute("INSERT INTO resource_recoveries VALUES(?,?,?,?,?,?,?)",
                       (identity, owner_id, idempotency_key, generation, json_dump({"state": "REQUESTED"}), stamp, stamp))
            return identity, generation, False
        identity, generation, reused = await self.rt.store.call(begin, transaction=True)
        call = await self.rt.store.call(lambda db: one(db,
            "SELECT connection_owner FROM mcp_call_resources WHERE call_id=?", (owner_id,)))
        if call:
            require(call.connection_owner == generation, "RESOURCE_GENERATION_CHANGED", "Recovery belongs to an earlier owner generation")
            result = await self.rt.mcp.capacity.recover(owner_id)
        else:
            lease = await self.rt.store.call(lambda db: one(db, "SELECT state,spec_json FROM resource_leases WHERE id=?", (owner_id,)))
            run_id = json.loads(lease.spec_json).get("owner_run_id") if lease else owner_id
            stopped = await self.inspect_execution_stop(run_id) if run_id else False
            if lease and run_id:
                lease = await self.rt.store.call(lambda db: one(db, "SELECT state FROM resource_leases WHERE id=?", (owner_id,)))
            state = ("RELEASED" if lease.state == "RELEASED" else "QUEUED" if lease.state == "QUEUED" else "NEEDS_EVIDENCE") if lease else (
                "EXECUTION_RELEASED" if stopped else "ACTIVE" if run_id in self.rt.active else
                "RELEASED" if owner_id not in self.owner_keys else "NEEDS_EVIDENCE")
            result = {"state": state, "generation": generation,
                      "reason": "Only observed resource stop boundaries permit automatic release"}
        result = {**result, "id": identity, "owner_id": owner_id}
        encoded = json_dump(result)
        await self.rt.store.call(lambda db: db.execute(
            "UPDATE resource_recoveries SET result_json=?,updated_at=? WHERE id=? AND result_json!=?",
            (encoded, now_ms(), identity, encoded)), transaction=True)
        return {**result, "reused": reused}
