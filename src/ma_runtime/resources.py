"""Atomic capacity admission and durable leases; the Runtime owns scheduling."""

import asyncio
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
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.phase_waiters = {}

    def define(self, key, limit, *, unit="operation", reserved=0):
        current = self.capacities.get(key)
        if current and (current.limit, current.unit, current.reserved) == (limit, unit, reserved):
            return
        self.capacities[key] = ResourceCapacity(key=key, limit=limit, unit=unit, reserved=reserved)

    def defaults(self, key, limit, *, unit="operation"):
        if key not in self.capacities:
            self.define(key, limit, unit=unit)

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
        for item in requirements:
            self.holders.setdefault(item.key, {})[owner] = item.mode
        for item in quantities:
            self.quantities.setdefault(item.key, {})[owner] = item.quantity

    def release(self, owner):
        if owner in self.isolated:
            return
        for collection in (self.holders, self.quantities):
            for key in tuple(collection):
                collection[key].pop(owner, None)
                if not collection[key]:
                    del collection[key]
        self.changed.set()
        if self.rt:
            self.rt.wake.set()

    def isolate(self, owner, reason):
        self.isolated[owner] = reason

    def resolve(self, owner):
        self.isolated.pop(owner, None)
        self.release(owner)

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
            if row.state == "QUEUED":
                self.pending[row.id] = spec
            else:
                self.restore(row.id, spec.resources, spec.quantities)
                self.isolate(row.id, "Lease owner must confirm execution stopped after host recovery")
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
                require(len(self.pending) + len(self.expirations) < self.rt.config.max_queued_tasks,
                        "QUEUE_FULL", "Resource lease queue has reached the configured pending-work limit")
                identity = new_id("lease")
                db.execute("INSERT INTO resource_leases(id,owner,scope,idempotency_key,request_hash,spec_json,state,created_at) VALUES(?,?,?,?,?,?,'QUEUED',?)",
                           (identity, spec.owner, spec.scope, spec.idempotency_key, digest, json_dump(spec), now_ms()))
                emit(db, "resource.requested", payload={"lease_id": identity, "owner": spec.owner, "scope": spec.scope, "phase": spec.phase})
                return identity, False
            self.reasons("", spec.resources, spec.quantities)
            identity, reused = await self.rt.store.call(insert, transaction=True)
            if not reused:
                self.pending[identity] = spec
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
            self.pending.pop(lease_id, None)
            self.expirations.pop(lease_id, None)
            self.resolve(lease_id)
        return await self.get(lease_id)

    async def tick(self):
        if not self.pending and not self.expirations:
            return
        async with self.lock:
            stamp = now_ms()
            for identity, expiry in tuple(self.expirations.items()):
                if expiry > stamp:
                    continue
                def expire(db, identity=identity):
                    db.execute("UPDATE resource_leases SET state='ISOLATED' WHERE id=? AND state='GRANTED'", (identity,))
                    emit(db, "resource.isolated", payload={"lease_id": identity, "reason": "LEASE_EXPIRED"})
                await self.rt.store.call(expire, transaction=True)
                self.expirations.pop(identity)
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
