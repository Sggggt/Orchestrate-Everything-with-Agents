"""Explicit, receipt-backed repair of a completed but incomplete delivery."""

import asyncio
import json

from .errors import require
from .models import ContinuationPolicy, TaskHandle, TaskSpec, content_hash, json_dump, now_ms
from .store import emit, one


class Continuations:
    def __init__(self, runtime):
        self.rt = runtime

    def snapshot(self, db, task_id):
        row = one(db, "SELECT * FROM task_continuations WHERE task_id=?", (task_id,))
        return {**{key: row[key] for key in ("runs_used", "calls_used", "tokens_reserved", "started_at", "deadline_at", "append_output")},
                **json.loads(row.policy_json)} if row else None

    async def begin(self, task_id, idempotency_key, content=""):
        self.rt.admit()
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256,
                "INVALID_REQUEST", "Use a stable continuation key")
        require(isinstance(content, str) and len(content) <= 4000, "INVALID_REQUEST", "Continuation guidance must contain at most 4000 characters")
        digest = content_hash({"content": content})
        def accept(db):
            from .services import need
            task = need(db, "tasks", task_id)
            prior = one(db, "SELECT request_hash FROM task_continuation_receipts WHERE task_id=? AND idempotency_key=?", (task_id, idempotency_key))
            if prior:
                require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Continuation key has different guidance")
                return TaskHandle(id=task_id, state=task.state, reused=True)
            thread = need(db, "threads", task.thread_id)
            spec = TaskSpec.model_validate_json(task.spec_json)
            require(need(db, "sessions", task.session_id).state == "OPEN", "SESSION_CLOSED", "Session is not open")
            require(task.state == "FAILED" and task.result_json, "INVALID_STATE", "Only a failed delivery with retained results can continue")
            error = json.loads(task.error_json or "{}")
            require(error.get("code") in {"OUTPUT_TRUNCATED", "EMPTY_OUTPUT", "OUTPUT_SCHEMA_MISMATCH", "COVERAGE_REQUIREMENT_UNMET", "ARTIFACT_REQUIREMENT_UNMET"},
                    "INVALID_STATE", "This failure requires inspection rather than delivery continuation")
            run = one(db, "SELECT * FROM runs WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1", (task_id,))
            receipt = json.loads(run.boundary_receipt_json) if run and run.boundary_receipt_json else {}
            checkpoint = receipt.get("checkpoint_id")
            require(checkpoint and thread.latest_checkpoint_id == checkpoint and not thread.active_run_id
                    and thread.owner_task_id in {None, task_id}, "REVISION_CONFLICT", "The conversation moved on or has no safe delivery checkpoint")
            require(not one(db, "SELECT 1 FROM tool_calls c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? AND c.state IN ('DISPATCHED','UNCERTAIN') LIMIT 1", (task_id,)),
                    "OUTCOME_UNCERTAIN", "Reconcile unresolved tool actions before continuing")
            require(self.rt.adapter(spec.agent_id).describe().supports_suspend, "UNSUPPORTED_CAPABILITY", "This Agent has no continuation checkpoint support")
            require(not spec.metadata.get("workflow_run_id"), "INVALID_STATE",
                    "Inspect the saved workflow result and submit an explicit repair task; its finalized run is retained")
            budget = one(db, "SELECT * FROM task_continuations WHERE task_id=?", (task_id,))
            policy = ContinuationPolicy.model_validate_json(budget.policy_json) if budget else spec.continuation_policy or ContinuationPolicy()
            require((budget.runs_used if budget else 0) < policy.max_runs, "CONTINUATION_BUDGET_EXCEEDED", "Continuation run budget is exhausted")
            stamp = now_ms()
            if budget:
                require(stamp < budget.deadline_at and budget.calls_used < policy.max_model_calls and budget.tokens_reserved < policy.max_output_tokens,
                        "CONTINUATION_BUDGET_EXCEEDED", "Continuation time or model budget is exhausted")
                db.execute("UPDATE task_continuations SET runs_used=runs_used+1 WHERE task_id=?", (task_id,))
            else:
                db.execute("INSERT INTO task_continuations VALUES(?,1,0,0,?,?,?,?)", (task_id, stamp,
                    stamp + int(policy.timeout_seconds * 1000), json_dump(policy), int(error.get("code") == "OUTPUT_TRUNCATED")))
            guidance = content or ("Complete the retained partial result without repeating its existing body." if (budget.append_output if budget else error.get("code") == "OUTPUT_TRUNCATED")
                                   else "Repair the missing delivery requirements and return the complete result.")
            db.execute("INSERT INTO task_continuation_receipts VALUES(?,?,?,?)", (task_id, idempotency_key, digest, stamp))
            db.execute("UPDATE tasks SET state='QUEUED',queue_reason='CONTINUATION',continuation_runs=continuation_runs+1,resume_input_json=?,not_before=0,updated_at=? WHERE id=?",
                       (json_dump({"continuation": True, "content": guidance}), stamp, task_id))
            db.execute("UPDATE threads SET owner_task_id=? WHERE id=?", (task_id, thread.id))
            emit(db, "task.continued", session_id=task.session_id, thread_id=thread.id, task_id=task_id,
                 payload={"state": "QUEUED"})
            return TaskHandle(id=task_id, state="QUEUED")
        result = await self.rt.store.call(accept, transaction=True)
        self.rt.notify()
        return result

    async def reserve_model(self, context, requested):
        if not context.request.continuation:
            return requested
        def reserve(db):
            task, _ = self.rt.check_run(db, context.request.run_id)
            budget = one(db, "SELECT * FROM task_continuations WHERE task_id=?", (task.id,))
            policy = ContinuationPolicy.model_validate_json(budget.policy_json)
            remaining = policy.max_output_tokens - budget.tokens_reserved
            require(now_ms() < budget.deadline_at and budget.calls_used < policy.max_model_calls and remaining > 0,
                    "CONTINUATION_BUDGET_EXCEEDED", "Continuation model budget is exhausted", details={"stage": "model"})
            allowed = min(requested or 4096, remaining)
            db.execute("UPDATE task_continuations SET calls_used=calls_used+1,tokens_reserved=tokens_reserved+? WHERE task_id=?", (allowed, task.id))
            return allowed
        return await self.rt.store.call(reserve, transaction=True)

    def settle_model(self, db, task_id, reserved, usage):
        actual = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if type(actual) is int and 0 <= actual <= reserved:
            db.execute("UPDATE task_continuations SET tokens_reserved=MAX(0,tokens_reserved-?) WHERE task_id=?",
                       (reserved - actual, task_id))

    def check_action(self, context, name, arguments, side_effects):
        if not context or not context.request.continuation:
            return
        require(now_ms() < context.request.continuation["deadline_at"], "CONTINUATION_BUDGET_EXCEEDED", "Continuation time budget is exhausted")
        if side_effects == "NONE":
            return
        path = arguments.get("path", "")
        require(name in {"write_file", "edit_file", "publish_artifact"} and isinstance(path, str)
                and (not path.startswith("/") or path.startswith("/workspace/")),
                "CONTINUATION_ACTION_RESTRICTED", "Continuation can repair workspace output but cannot start new external actions")

    async def merge_output(self, context, text):
        if not context.request.continuation or not context.request.continuation["append_output"]:
            return text
        prefix = getattr(context, "_continuation_prefix", None)
        if prefix is None:
            result = await self.rt.store.call(lambda db: json.loads(one(db, "SELECT result_json FROM tasks WHERE id=?", (context.request.task_id,)).result_json))
            output = result.get("output", {})
            reference = output.get("full_text_artifact_id")
            if reference:
                artifact = await self.rt.artifacts.get(reference)
                require(artifact.session_id == context.request.session_id, "PERMISSION_DENIED", "Continuation output belongs to another session")
                path = await self.rt.artifacts.open(reference)
                prefix = await asyncio.to_thread(path.read_text, encoding="utf-8")
            else:
                prefix = output.get("text", "")
                if not prefix:
                    prefix = result.get("metadata", {}).get("partial_model_output", {}).get("text", "")
            context._continuation_prefix = prefix if isinstance(prefix, str) else ""
            prefix = context._continuation_prefix
        if text.startswith(prefix) or not prefix:
            return text
        overlap = next((size for size in range(min(1024, len(prefix), len(text)), 0, -1) if prefix.endswith(text[:size])), 0)
        return prefix + text[overlap:]
