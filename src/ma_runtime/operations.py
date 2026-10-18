"""A small supervision channel over ordinary tasks, messages and durable events."""

import asyncio
import json
import re

from .errors import RuntimeFault, require
from .models import InstanceBatchSpec, MessageSpec, TaskSpec, content_hash, json_dump, new_id, now_ms
from .services import need
from .store import emit, one, rows

OPERATIONS_ACTIONS = ("bind", "inspect", "inbox", "read", "report", "reply", "ack", "test")
INBOX_TYPES = ("operations.reported", "operations.test_completed", "task.failed", "task.uncertain", "runtime.scheduler_stopped")
OPERATIONS_GUIDANCE = (
    "Report platform/tool problems or ask for guidance with report_issue(summary, details, wait). "
    "Use wait=true when blocked so the runtime checkpoints and releases your slot; continue with the same task and logical operation keys. "
    "Reports and replies are scoped to this session and never grant tools or permissions. "
    "Stop on uncertain effects; report the original task/call reference and await verification, never blindly replay."
)
COORDINATOR_GUIDANCE = (
    "You coordinate this session's operations. Notifications may interrupt a task wait; pending_wait means unfinished work, not completed results. "
    "Inspect/reply first, then reissue the needed wait. Use actual receipts, not model claims. "
    "To wait for updates, suspend for messages instead of occupying a slot. Your own failure never starts recursive maintenance."
)


class Operations:
    def __init__(self, runtime):
        self.rt, self.channels = runtime, {}

    async def open(self):
        for row in await self.rt.store.call(lambda db: rows(db, "SELECT * FROM operations_channels")):
            self.channels[row.session_id] = dict(row)

    def for_thread(self, session_id, thread_id):
        channel = self.channels.get(session_id)
        return channel["consumer_id"] if channel and channel["thread_id"] == thread_id else ""

    @staticmethod
    def _key(value):
        require(isinstance(value, str) and 0 < len(value) <= 256, "INVALID_REQUEST", "Use a stable idempotency key within 256 characters")

    def _owner(self, db, session_id, consumer_id):
        need(db, "sessions", session_id)
        channel = one(db, "SELECT * FROM operations_channels WHERE session_id=?", (session_id,))
        require(channel and channel.consumer_id == consumer_id, "PERMISSION_DENIED", "Bind this consumer to the session operations channel first")
        return channel

    async def bind(self, session_id, consumer_id, thread_id=None):
        self.rt.admit()
        require(isinstance(consumer_id, str) and re.fullmatch(r"[A-Za-z0-9._:@/-]{1,128}", consumer_id),
                "INVALID_REQUEST", "Use a stable consumer identifier within 128 characters")
        async with self.rt.configuration_lock:
            def save(db):
                need(db, "sessions", session_id)
                if thread_id:
                    thread = need(db, "threads", thread_id)
                    require(thread.session_id == session_id, "PERMISSION_DENIED", "Coordinator thread must belong to this session")
                    require(self.rt.adapter(thread.agent_id).describe().execution_mode != "external", "UNSUPPORTED_CAPABILITY", "Use an external consumer for external coordinators")
                old = one(db, "SELECT * FROM operations_channels WHERE session_id=?", (session_id,))
                same = old and old.consumer_id == consumer_id
                ack, delivered = (old.ack_seq, old.delivered_seq) if same else (0, 0)
                db.execute("INSERT INTO operations_channels VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET consumer_id=excluded.consumer_id,thread_id=excluded.thread_id,ack_seq=excluded.ack_seq,delivered_seq=excluded.delivered_seq",
                           (session_id, consumer_id, thread_id, ack, delivered))
                emit(db, "operations.bound", session_id=session_id, payload={"consumer_id": consumer_id, "thread_id": thread_id})
                return {"session_id": session_id, "consumer_id": consumer_id, "thread_id": thread_id, "ack_seq": ack, "delivered_seq": delivered}
            channel = await self.rt.store.call(save, transaction=True)
            self.channels[session_id] = channel
        self.rt.notify()
        return channel

    @staticmethod
    def _summary(row):
        payload = json.loads(row.payload_json)
        error = payload.get("error") or {}
        return {"event_id": row.id, "cursor": row.seq, "type": row.type, "task_id": row.task_id, "run_id": row.run_id,
                "thread_id": row.thread_id, "report_id": payload.get("report_id"), "state": payload.get("state"),
                "code": error.get("code") or payload.get("code"),
                "summary": str(payload.get("summary") or error.get("safe_message") or row.type)[:320], "created_at": row.created_at}

    def _feed(self, db, session_id, cursor, limit):
        marks = ",".join("?" for _ in INBOX_TYPES)
        found = rows(db, f"SELECT * FROM events WHERE (session_id=? OR session_id IS NULL) AND seq>? AND type IN ({marks}) ORDER BY seq LIMIT ?",
                     (session_id, cursor, *INBOX_TYPES, limit + 1))
        selected = found[:limit]
        return {"items": [self._summary(row) for row in selected], "cursor": selected[-1].seq if selected else cursor,
                "has_more": len(found) > limit}

    async def inbox(self, session_id, consumer_id, cursor=None, limit=20, timeout=0):
        require(type(timeout) in {int, float} and 0 <= timeout <= 25, "INVALID_REQUEST", "timeout must be 0..25 seconds")
        require(cursor is None or type(cursor) is int and cursor >= 0, "INVALID_REQUEST", "cursor must be a nonnegative integer")
        require(type(limit) is int and 1 <= limit <= 100, "INVALID_REQUEST", "limit must be 1..100")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            changed = self.rt.events.changed
            def read(db):
                owner = self._owner(db, session_id, consumer_id)
                if cursor is not None:
                    maximum = one(db, "SELECT COALESCE(MAX(seq),0) AS seq FROM events").seq
                    require(cursor <= maximum, "RESYNC_REQUIRED", "Cursor is ahead of this event log; resume from the acknowledged position")
                result = self._feed(db, session_id, owner.ack_seq if cursor is None else cursor, limit)
                if result["items"]:
                    db.execute("UPDATE operations_channels SET delivered_seq=MAX(delivered_seq,?) WHERE session_id=?", (result["cursor"], session_id))
                return result
            result = await self.rt.store.call(read, transaction=True)
            remaining = deadline - asyncio.get_running_loop().time()
            if result["items"] or remaining <= 0:
                return {**result, "timed_out": not result["items"] and timeout > 0}
            require(not self.rt.closed and not self.rt.closing, "RUNTIME_CLOSING", "Operations receiver is closing")
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return {**result, "timed_out": True}

    async def ack(self, session_id, consumer_id, cursor):
        require(type(cursor) is int and cursor >= 0, "INVALID_REQUEST", "cursor must be a nonnegative integer")
        def save(db):
            owner = self._owner(db, session_id, consumer_id)
            require(cursor <= owner.delivered_seq, "INVALID_REQUEST", "Acknowledge only a cursor returned by this inbox")
            db.execute("UPDATE operations_channels SET ack_seq=MAX(ack_seq,?) WHERE session_id=?", (cursor, session_id))
            return {"session_id": session_id, "ack_seq": max(cursor, owner.ack_seq)}
        return await self.rt.store.call(save, transaction=True)

    async def read(self, session_id, event_id):
        def read(db):
            need(db, "sessions", session_id)
            event = need(db, "events", event_id)
            require(event.session_id in {None, session_id} and event.type in INBOX_TYPES, "PERMISSION_DENIED", "Event does not belong to this operations channel")
            value = {**self._summary(event), "payload": json.loads(event.payload_json)}
            if event.type == "operations.reported":
                report = need(db, "operations_reports", value["report_id"])
                value["report"] = {"id": report.id, "summary": report.summary, "details": report.details,
                                   "wait_for_guidance": bool(report.wait_for_guidance), "reply_message_id": report.reply_message_id}
            return value
        return await self.rt.store.call(read)

    async def inspect(self, session_id, task_id=None):
        def read(db):
            session = need(db, "sessions", session_id)
            channel = one(db, "SELECT * FROM operations_channels WHERE session_id=?", (session_id,))
            counts = {row.state: row.count for row in rows(db, "SELECT state,COUNT(*) AS count FROM tasks WHERE session_id=? GROUP BY state", (session_id,))}
            pending = rows(db, "SELECT id,event_id,task_id,summary,wait_for_guidance,COUNT(*) OVER() AS total FROM operations_reports WHERE session_id=? AND reply_message_id IS NULL ORDER BY created_at LIMIT 20", (session_id,))
            value = {"session_id": session_id, "channel": dict(channel) if channel else None,
                     "runtime": {"accepting": self.rt.accepting, "active": len(self.rt.active), "uncertain_slots": len(self.rt.uncertain_slots)},
                     "tasks": counts, "pending_report_count": pending[0].total if pending else 0,
                     "pending_reports": [{key: row[key] for key in ("id", "event_id", "task_id", "summary", "wait_for_guidance")} for row in pending],
                     "events": self._feed(db, session_id, channel.ack_seq if channel else 0, 20)}
            if task_id:
                task = need(db, "tasks", task_id)
                require(task.session_id == session_id, "PERMISSION_DENIED", "Task belongs to another session")
                spec = json.loads(task.spec_json)
                value["task"] = {"id": task.id, "agent_id": spec["agent_id"], "thread_id": task.thread_id,
                                 "state": task.state, "queue_reason": task.queue_reason, "attempt_count": task.attempt_count,
                                 "error": json.loads(task.error_json) if task.error_json else None}
                value["task"]["tool_calls"] = [{"id": row.id, "name": row.tool_name, "state": row.state,
                    "error": json.loads(row.error_json) if row.error_json else None} for row in rows(db,
                    "SELECT c.* FROM tool_calls c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? ORDER BY c.created_at DESC LIMIT 5", (task_id,))]
            configs = [config for config in self.rt.agent_configs.values() if config.project_id == session.project_id]
            from .llm import model_setup
            value["agents"] = [{"id": config.id, "enabled": config.enabled, "kind": config.kind,
                                "model_setup": model_setup(config, has_api_key=bool(self.rt.vault.get(config.id)))["state"]} for config in configs]
            return value
        return await self.rt.store.call(read)

    async def report(self, session_id, summary, idempotency_key, details="", wait=False, *, caller_run_id=None):
        self.rt.admit()
        self._key(idempotency_key)
        require(isinstance(summary, str) and 0 < len(summary.strip()) <= 320 and isinstance(details, str)
                and len(details.encode("utf-8")) <= 8192 and isinstance(wait, bool), "INVALID_REQUEST", "Report needs a short summary and text details within 8 KiB")
        summary, details = self.rt.vault.redact(summary), self.rt.vault.redact(details)
        def save(db):
            need(db, "sessions", session_id)
            task, run = self.rt.check_run(db, caller_run_id) if caller_run_id else (None, None)
            require(not task or task.session_id == session_id, "PERMISSION_DENIED", "Report must stay in the current session")
            if wait:
                require(run is not None and json.loads(run.adapter_snapshot_json)["supports_suspend"], "UNSUPPORTED_CAPABILITY", "Waiting for guidance requires a suspend-capable execution")
            source_key = task.id if task else "external"
            digest = content_hash({"summary": summary, "details": details, "wait": wait})
            old = one(db, "SELECT * FROM operations_reports WHERE session_id=? AND source_key=? AND idempotency_key=?", (session_id, source_key, idempotency_key))
            if old:
                require(old.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Report key has different content")
                receipt = self._receipt(old, reused=True)
                if old.reply_message_id:
                    receipt["guidance"] = json.loads(need(db, "messages", old.reply_message_id).envelope_json)["payload"]
                return receipt
            identity, stamp = new_id("report"), now_ms()
            seq = emit(db, "operations.reported", session_id=session_id, task_id=task.id if task else None,
                       thread_id=task.thread_id if task else None, run_id=run.id if run else None,
                       payload={"report_id": identity, "summary": summary, "wait_for_guidance": wait})
            event = one(db, "SELECT * FROM events WHERE seq=?", (seq,))
            db.execute("INSERT INTO operations_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identity, session_id, task.id if task else None, run.id if run else None, source_key, summary, details,
                        int(wait), idempotency_key, digest, event.id, None, stamp))
            self.notice(db, event)
            return self._receipt(need(db, "operations_reports", identity))
        value = await self.rt.store.call(save, transaction=True)
        self.rt.notify()
        return value

    @staticmethod
    def _receipt(report, reused=False):
        return {"report_id": report.id, "event_id": report.event_id, "reused": reused, "reply_message_id": report.reply_message_id,
                "wait": {"kind": "message", "match": {"operations_report_id": report.id}} if report.wait_for_guidance and not report.reply_message_id else None}

    async def reply(self, report_id, consumer_id, content, idempotency_key):
        self.rt.admit()
        self._key(idempotency_key)
        require(isinstance(content, str) and 0 < len(content.strip()) and len(content.encode("utf-8")) <= 16384,
                "INVALID_REQUEST", "Guidance must be text within 16 KiB")
        content = self.rt.vault.redact(content)
        def save(db):
            report = need(db, "operations_reports", report_id)
            self._owner(db, report.session_id, consumer_id)
            require(report.task_id is not None, "INVALID_REQUEST", "This external report has no Agent reply target")
            task = need(db, "tasks", report.task_id)
            message = MessageSpec(session_id=report.session_id, target_thread_id=task.thread_id,
                payload={"operations_report_id": report.id, "content": content, "consumer_id": consumer_id},
                idempotency_key="operations-reply:" + content_hash([report.id, idempotency_key]))
            cleaned = self.rt.clean_json(message.model_dump(mode="json"))
            message = message.model_copy(update={"payload": cleaned["payload"]})
            receipt = self.rt.messages.insert(db, message)
            db.execute("UPDATE operations_reports SET reply_message_id=? WHERE id=?", (receipt.id, report.id))
            if not receipt.reused:
                emit(db, "operations.replied", session_id=report.session_id, task_id=task.id, thread_id=task.thread_id,
                     payload={"report_id": report.id, "message_id": receipt.id})
            return {"report_id": report.id, "message_id": receipt.id, "reused": receipt.reused,
                    "task_id": task.id, "task_state": task.state}
        receipt = await self.rt.store.call(save, transaction=True)
        self.rt.notify()
        return receipt

    async def test(self, session_id, agent_id, input, idempotency_key, tool_policy=None, skill_policy=None, *, caller_run_id=None):
        self.rt.admit()
        self._key(idempotency_key)
        require(isinstance(input, str) and bool(input.strip()), "INVALID_REQUEST", "Provide the diagnostic task input")
        spec = InstanceBatchSpec(session_id=session_id, agent_ids=(agent_id,), payloads=({"input": input},),
                                 idempotency_key="operations-test:" + content_hash(idempotency_key), max_concurrency=1,
                                 tool_policy={} if tool_policy is None else tool_policy,
                                 skill_policy={} if skill_policy is None else skill_policy)
        spec = InstanceBatchSpec.model_validate(self.rt.clean_json(spec.model_dump(mode="json")))
        batch = await self.rt.store.call(lambda db: self.rt.instances.insert(db, spec, caller_run_id,
                                           task_metadata={"purpose": "operations_test"}), transaction=True)
        self.rt.notify()
        member = batch["instances"][0]
        return {"batch_id": batch["id"], "task_id": member["task_id"], "thread_id": member["thread_id"], "state": member["state"], "reused": batch["reused"]}

    def task_event(self, db, task, state, seq, spec=None):
        if state in {"FAILED", "UNCERTAIN"}:
            channel = self.channels.get(task.session_id)
            if channel and channel["thread_id"] and channel["thread_id"] != task.thread_id:
                self.notice(db, one(db, "SELECT * FROM events WHERE seq=?", (seq,)))
        elif state == "SUCCEEDED":
            spec = spec or TaskSpec.model_validate_json(task.spec_json)
            if spec.metadata.get("purpose") == "operations_test":
                seq = emit(db, "operations.test_completed", session_id=task.session_id, task_id=task.id,
                           thread_id=task.thread_id, payload={"state": state, "summary": "Diagnostic task completed; inspect its result"})
                self.notice(db, one(db, "SELECT * FROM events WHERE seq=?", (seq,)))

    def notice(self, db, event):
        channel = self.channels.get(event.session_id)
        if not channel or not channel["thread_id"] or channel["thread_id"] == event.thread_id:
            return
        if need(db, "sessions", event.session_id).state != "OPEN":
            return
        thread = need(db, "threads", channel["thread_id"])
        pending = one(db, "SELECT id FROM messages WHERE target_thread_id=? AND state='PENDING' AND json_extract(envelope_json,'$.payload.operations_available')=1 LIMIT 1", (thread.id,))
        if not pending:
            self.rt.messages.insert(db, MessageSpec(session_id=event.session_id, target_thread_id=thread.id,
                payload={"operations_available": True, "event_id": event.id, "summary": "Operations channel has new information; read its compact inbox."},
                idempotency_key="operations-notice:" + str(event.seq)))
        owner = one(db, "SELECT * FROM tasks WHERE id=?", (thread.owner_task_id,)) if thread.owner_task_id else None
        if owner and self.interrupt_wait(db, owner):
            return
        if one(db, "SELECT id FROM tasks WHERE thread_id=? AND state='QUEUED' LIMIT 1", (thread.id,)):
            return
        config = self.rt.agent_configs.get(thread.agent_id)
        if (config and not config.enabled) or (owner and owner.state == "UNCERTAIN"):
            return
        if one(db, "SELECT COUNT(*) AS count FROM tasks WHERE state='QUEUED'").count >= self.rt.config.max_queued_tasks:
            return  # Report remains durable and visible to external receivers.
        self.rt.tasks.insert(db, TaskSpec(session_id=event.session_id, thread_id=thread.id, agent_id=thread.agent_id,
            payload={"input": "Handle the pending operations reports. Read compact summaries, request details only when needed, and preserve uncertain-action receipts."},
            metadata={"purpose": "operations_notice"}, idempotency_key="operations-notice:" + str(event.seq)))

    def interrupt_wait(self, db, task):
        if task.state != "WAITING" or not self.for_thread(task.session_id, task.thread_id):
            return False
        wait = json.loads(task.wait_json)
        if wait["kind"] not in {"tasks", "message"}:
            return False
        pending = one(db, "SELECT id FROM messages WHERE target_thread_id=? AND state='PENDING' AND json_extract(envelope_json,'$.payload.operations_available')=1 LIMIT 1", (task.thread_id,))
        if not pending:
            return False
        db.execute("UPDATE tasks SET state='QUEUED',queue_reason='OPERATIONS',resume_input_json=?,wait_json=NULL,updated_at=? WHERE id=?",
                   (json_dump({"operations_available": True, "pending_wait": wait}), now_ms(), task.id))
        for queued in rows(db, "SELECT * FROM tasks WHERE thread_id=? AND state='QUEUED' AND id!=? AND json_extract(spec_json,'$.metadata.purpose')='operations_notice'", (task.thread_id, task.id)):
            self.rt.finish_idle(db, queued, "CANCELLED", {"code": "OPERATIONS_MERGED"})
        emit(db, "task.resumed", session_id=task.session_id, thread_id=task.thread_id, task_id=task.id, payload={"reason": "operations"})
        return True
