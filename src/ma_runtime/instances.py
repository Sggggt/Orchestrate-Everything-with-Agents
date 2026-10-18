"""Durable batches of independent threads, executed by the ordinary scheduler."""

import asyncio
import json
from collections import Counter

from .errors import RuntimeFault, require
from .models import InstanceBatchSpec, PageEnvelope, TaskSpec, TERMINAL, content_hash, json_dump, new_id, now_ms
from .services import agent_project, need, resolve_project
from .store import emit, one, rows
from .llm import model_required


class Instances:
    def __init__(self, runtime):
        self.rt = runtime

    def validate_agents(self, db, project_id, identities):
        require(bool(identities) and len(set(identities)) == len(identities),
                "INVALID_REQUEST", "Choose a nonempty pool of unique Agents")
        for identity in identities:
            adapter = self.rt.adapter(identity)
            require(agent_project(db, identity) == project_id, "PROJECT_MISMATCH",
                    "Instance Agents must belong to the session project")
            require(adapter.describe().execution_mode != "external", "UNSUPPORTED_CAPABILITY",
                    "Instances require platform-managed Agents")

    def insert(self, db, spec, caller_run_id=None, *, task_metadata=None):
        parent = None
        if caller_run_id:
            parent, run = self.rt.check_run(db, caller_run_id)
            self.rt.check_collaboration(run)
            require(parent.session_id == spec.session_id, "PERMISSION_DENIED",
                    "Child instances must stay in the current session")
        parent_id = parent.id if parent else None
        from .execution_policy import request_hash
        digest = request_hash(spec)
        if task_metadata:
            digest = content_hash({"request_hash": digest, "metadata": task_metadata})
        old = one(db, "SELECT * FROM instance_batches WHERE session_id=? AND idempotency_key=?",
                  (spec.session_id, spec.idempotency_key))
        if old:
            require(old.request_hash == digest and old.parent_task_id == parent_id,
                    "IDEMPOTENCY_CONFLICT", "Batch key has different input or parent")
            return self.snapshot(db, old.id, reused=True)
        session = need(db, "sessions", spec.session_id)
        resolve_project(db, session.project_id)
        require(session.state == "OPEN" or (parent and session.state == "CLOSING" and session.close_mode == "drain"),
                "SESSION_CLOSED", "Session is closing")
        self.validate_agents(db, session.project_id, spec.agent_ids)
        queued = one(db, "SELECT COUNT(*) AS count FROM tasks WHERE state='QUEUED'").count
        require(queued + len(spec.payloads) <= self.rt.config.max_queued_tasks,
                "QUEUE_FULL", "Not enough queue capacity for the complete batch")
        loads = Counter({row.agent_id: row.count for row in rows(db,
            "SELECT h.agent_id,COUNT(*) AS count FROM tasks t JOIN threads h ON h.id=t.thread_id "
            "WHERE t.state IN ('QUEUED','RUNNING','UNCERTAIN') GROUP BY h.agent_id")})
        capacities = {identity: min(self.rt.adapter(identity).describe().max_concurrency,
            self.rt.agent_configs[identity].max_concurrency if identity in self.rt.agent_configs else 64)
            for identity in spec.agent_ids}
        unavailable = {identity for identity in spec.agent_ids
            if (config := self.rt.agent_configs.get(identity)) and
            (not config.enabled or (model_required(config) and not (config.base_url and config.model)))}
        identity, stamp = new_id("batch"), now_ms()
        db.execute("INSERT INTO instance_batches(id,session_id,parent_task_id,idempotency_key,request_hash,max_concurrency,routing,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (identity, spec.session_id, parent_id, spec.idempotency_key, digest, spec.max_concurrency, spec.routing, stamp))
        for ordinal, payload in enumerate(spec.payloads):
            agent_id = (spec.agent_ids[ordinal % len(spec.agent_ids)] if spec.routing == "round_robin"
                        else min(spec.agent_ids, key=lambda candidate: (candidate in unavailable,
                                                                      loads[candidate] / capacities[candidate])))
            loads[agent_id] += 1
            thread_id = new_id("thr")
            db.execute("INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,created_at) VALUES(?,?,?,?,?,?)",
                       (thread_id, spec.session_id, agent_id, f"{agent_id} · {ordinal + 1}",
                        json_dump(self.rt.adapter(agent_id).describe()), stamp))
            emit(db, "thread.created", session_id=spec.session_id, thread_id=thread_id,
                 payload={"agent_id": agent_id, "batch_id": identity})
            task = TaskSpec(session_id=spec.session_id, thread_id=thread_id, agent_id=agent_id,
                payload=payload, idempotency_key=f"{identity}:{ordinal}", depends_on=spec.depends_on,
                attempt_timeout_s=spec.attempt_timeout_s, max_attempts=spec.max_attempts,
                retry_policy=spec.retry_policy, metadata={"batch_id": identity, "instance_index": ordinal,
                                                         "parent_task_id": parent_id, **(task_metadata or {})},
                tool_policy=spec.tool_policy, skill_policy=spec.skill_policy)
            handle = self.rt.tasks.insert(db, task, caller_run_id=caller_run_id)
            db.execute("INSERT INTO instance_members VALUES(?,?,?)", (identity, ordinal, handle.id))
        emit(db, "instances.spawned", session_id=spec.session_id,
             payload={"batch_id": identity, "count": len(spec.payloads), "parent_task_id": parent_id})
        return self.snapshot(db, identity)

    async def spawn(self, spec: InstanceBatchSpec | dict, *, caller_run_id=None):
        self.rt.admit()
        spec = InstanceBatchSpec.model_validate(spec)
        spec = InstanceBatchSpec.model_validate(self.rt.clean_json(spec.model_dump(mode="json")))
        result = await self.rt.store.call(lambda db: self.insert(db, spec, caller_run_id), transaction=True)
        self.rt.notify()
        return result

    def snapshot(self, db, batch_id, *, reused=False, include_outputs=False):
        batch = need(db, "instance_batches", batch_id)
        outputs = ",t.result_json,t.error_json" if include_outputs else ""
        members = rows(db, "SELECT m.ordinal,t.id AS task_id,t.thread_id,h.agent_id,t.state,t.queue_reason" + outputs + " "
                       "FROM instance_members m JOIN tasks t ON t.id=m.task_id JOIN threads h ON h.id=t.thread_id "
                       "WHERE m.batch_id=? ORDER BY m.ordinal", (batch_id,))
        states = {member.state for member in members}
        settled = states <= TERMINAL
        if "UNCERTAIN" in states:
            state = "UNCERTAIN"
        elif batch.cancel_requested_at is not None:
            state = "CANCELLED" if settled else "CANCELLING"
        elif settled:
            state = "SUCCEEDED" if states == {"SUCCEEDED"} else "FAILED"
        else:
            state = "WAITING" if states - TERMINAL == {"WAITING"} else "RUNNING"
        instances = []
        for member in members:
            item = {key: member[key] for key in ("ordinal", "task_id", "thread_id", "agent_id", "state", "queue_reason")}
            if include_outputs:
                item["result"] = json.loads(member.result_json) if member.result_json else None
                item["error"] = json.loads(member.error_json) if member.error_json else None
            instances.append(item)
        return {"id": batch.id, "session_id": batch.session_id, "parent_task_id": batch.parent_task_id,
                "state": state, "max_concurrency": batch.max_concurrency, "routing": batch.routing,
                "created_at": batch.created_at, "reused": reused, "counts": dict(Counter(m.state for m in members)),
                "instances": instances}

    async def get(self, batch_id, include_outputs=False):
        return await self.rt.store.call(lambda db: self.snapshot(db, batch_id, include_outputs=include_outputs))

    async def list(self, session_id, cursor=None, limit=25):
        await self.rt.sessions.get(session_id)
        limit = max(1, min(int(limit), 100))
        def read(db):
            found = rows(db, "SELECT rowid AS position,* FROM instance_batches WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                         (session_id, int(cursor or 0), limit + 1))
            items = [{key: value for key, value in self.snapshot(db, row.id).items() if key != "instances"}
                     for row in found[:limit]]
            return PageEnvelope(items=items, has_more=len(found) > limit,
                                cursor=str(found[limit - 1].position) if len(found) > limit else None)
        return await self.rt.store.call(read)

    async def result(self, batch_id, timeout=0):
        require(isinstance(timeout, (int, float)) and 0 <= timeout <= 120,
                "INVALID_REQUEST", "Timeout must be between 0 and 120 seconds")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            batch = await self.get(batch_id)
            if batch["state"] == "SUCCEEDED":
                return await self.get(batch_id, include_outputs=True)
            if batch["state"] in {"FAILED", "CANCELLED", "UNCERTAIN"}:
                raise RuntimeFault("OUTCOME_UNCERTAIN" if batch["state"] == "UNCERTAIN" else "BATCH_" + batch["state"],
                                   "Inspect instance results before deciding recovery", details={"batch_id": batch_id})
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeFault("WAIT_TIMEOUT", "Instances are still running", retryable=True)
            await asyncio.sleep(0.05)

    def cancel_in_transaction(self, db, batch_id):
        batch = need(db, "instance_batches", batch_id)
        if self.snapshot(db, batch_id)["state"] in TERMINAL:
            return set()
        stamp = now_ms()
        db.execute("UPDATE instance_batches SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE id=?", (stamp, batch_id))
        pending = rows(db, "SELECT t.* FROM tasks t JOIN instance_members m ON m.task_id=t.id "
                       "WHERE m.batch_id=? AND t.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED')", (batch_id,))
        for task in pending:
            if task.cancel_requested_at is None:
                db.execute("UPDATE tasks SET cancel_requested_at=?,updated_at=? WHERE id=?", (stamp, stamp, task.id))
                emit(db, "task.cancel_requested", session_id=batch.session_id, thread_id=task.thread_id, task_id=task.id)
            if task.state in {"QUEUED", "WAITING"}:
                self.rt.finish_idle(db, task, "CANCELLED", None)
        return {task.id for task in pending}

    def signal_cancel(self, task_ids):
        for active in self.rt.active.values():
            if active.request.task_id in task_ids:
                active.context.cancelled.set()

    async def cancel(self, batch_id):
        self.rt.admit()
        self.signal_cancel(await self.rt.store.call(lambda db: self.cancel_in_transaction(db, batch_id), transaction=True))
        self.rt.notify()
        result = await self.get(batch_id)
        return {**result, "confirmed_stopped": result["state"] in TERMINAL}

    def cancel_children(self, db):
        cancelled = set()
        while True:
            batches = rows(db, "SELECT b.id FROM instance_batches b JOIN tasks p ON p.id=b.parent_task_id "
                "WHERE b.cancel_requested_at IS NULL AND (p.cancel_requested_at IS NOT NULL OR p.state IN ('FAILED','CANCELLED','SKIPPED')) "
                "AND EXISTS(SELECT 1 FROM instance_members m JOIN tasks t ON t.id=m.task_id WHERE m.batch_id=b.id "
                "AND t.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED'))")
            if not batches:
                break
            for batch in batches:
                cancelled.update(self.cancel_in_transaction(db, batch.id))
        return cancelled
