"""Authorized tool calls with stable receipts and explicit unknown outcomes."""

import asyncio
import hashlib
import json
from dataclasses import dataclass

from jsonschema import ValidationError

from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, new_id, now_ms, one
from oea.contracts.schema_validation import check_schema, schema_validator, validate_value


@dataclass(frozen=True)
class PreparedTool:
    name: str
    arguments: dict
    encoded: str
    fingerprint: str
    bound_arguments: object | None = None


class ToolGateway:
    def __init__(self, store, *, authorize, record_usage=None, record_progress=None, redact=lambda value: value, on_settle=None, authorize_intent=None, on_intent_settle=None):
        self.store = store
        self.authorize = authorize
        self.record_usage = record_usage
        self.record_progress = record_progress
        self.redact = redact
        self.on_settle = on_settle
        self.authorize_intent, self.on_intent_settle = authorize_intent, on_intent_settle
        self.providers = {}
        self.validators = {}
        self.contracts = {}
        self.inflight = {}
        self.names = {}
        self.owners = {}
        self.started = set()
        self.cancel_requested = set()

    def register(self, provider):
        spec = provider.describe()
        require(spec.name not in self.providers, "TOOL_EXISTS", "A tool name has one provider")
        for schema in (spec.input_schema, spec.output_schema):
            check_schema(schema)
        self.providers[spec.name] = provider
        self.validators[spec.name] = tuple(schema_validator(schema) for schema in (spec.input_schema, spec.output_schema))
        encoded = encode(spec)
        self.contracts[spec.name] = (hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded)

    def catalog(self, names):
        result = []
        for name in names:
            provider = self.providers.get(name)
            if provider is not None:
                spec = provider.describe()
                result.append({"type": "function", "function": {"name": name, "description": spec.description, "parameters": spec.input_schema}})
        return result

    def prepare(self, name, arguments):
        require(name in self.providers, "TOOL_UNAVAILABLE", "Tool is not bound to this host")
        try:
            validate_value(self.validators[name][0], arguments)
            encoded = encode(arguments)
        except (ValidationError, TypeError, ValueError):
            raise RuntimeFault("INVALID_REQUEST", "Tool input must be JSON matching its schema") from None
        # Snapshot the external input once; receipt and provider see one value.
        snapshot = json.loads(encoded)
        bind = getattr(self.providers[name], "bind_arguments", None)
        bound = bind(snapshot) if bind is not None else None
        return PreparedTool(name, snapshot, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), bound)

    async def invoke(self, run_id, name, arguments, operation_key):
        await self.authorize(run_id, name)
        return await self._invoke(run_id, self.prepare(name, arguments), operation_key)

    async def invoke_intent(self, intent_id, prepared, operation_key, *, wait=True):
        require(self.authorize_intent is not None, "PERMISSION_DENIED", "Controller tool intents are unavailable")
        return await self._invoke(None, prepared, operation_key, intent_id=intent_id, wait=wait)

    @staticmethod
    def guard(db, run_id, intent_id):
        if run_id is not None:
            owner = one(db, "SELECT * FROM runs WHERE id=?", (run_id,))
            require(owner is not None and owner["state"] == "running" and owner["cancel_requested_at"] is None,
                    "STALE_RUN", "Run is no longer allowed to execute tools", run_id=run_id)
        else:
            owner = one(db, "SELECT * FROM tool_intents WHERE id=?", (intent_id,))
            require(owner is not None and owner["cancel_requested_at"] is None,
                    "CALL_CANCELLED", "Controller intent no longer permits dispatch", call_id=intent_id)
        return owner

    async def _invoke(self, run_id, prepared, operation_key, *, intent_id=None, wait=True):
        name, arguments, encoded, digest = prepared.name, prepared.arguments, prepared.encoded, prepared.fingerprint
        provider, spec = self.providers[name], self.providers[name].describe()

        def intent(db):
            owner = self.guard(db, run_id, intent_id)
            require(run_id is not None or owner["name"] == name and owner["input_hash"] == digest,
                    "IDEMPOTENCY_CONFLICT", "Controller intent has different tool input", call_id=intent_id)
            previous = one(db, "SELECT * FROM actions WHERE run_id=? AND operation_key=?", (run_id, operation_key)) if run_id is not None else one(db, "SELECT * FROM actions WHERE intent_id=?", (intent_id,))
            if previous:
                require(previous["operation_key"] == operation_key and previous["name"] == name and previous["input_hash"] == digest,
                        "IDEMPOTENCY_CONFLICT", "Action key has different input", run_id=run_id, action_id=previous["id"])
                return previous, False
            identity = new_id("call")
            if self.record_usage and run_id is not None:
                self.record_usage(db, owner, identity, name)
            db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,error_json,created_at,updated_at,intent_id) VALUES(?,?,?,?,?,?,?,'intent',NULL,NULL,?,?,?)",
                       (identity, run_id, operation_key, name, digest, encode(self.redact(arguments)) if run_id is not None else None, spec.side_effects, now_ms(), now_ms(), intent_id))
            contract_id, contract = self.contracts[name]
            db.execute("INSERT OR IGNORE INTO tool_contracts VALUES(?,?)", (contract_id, contract))
            db.execute("INSERT INTO action_contracts VALUES(?,?)", (identity, contract_id))
            event(db, "action.accepted", run_id=run_id, intent_id=intent_id, data={"action_id": identity, "tool": name})
            return one(db, "SELECT * FROM actions WHERE id=?", (identity,)), True

        receipt, created = await self.store.call(intent, transaction=True)
        identity = receipt["id"]
        if not created:
            if not wait and (receipt["state"] != "intent" or identity in self.inflight):
                return receipt
            authorize_replay = getattr(provider, "authorize_replay", None)
            if authorize_replay is not None:
                await authorize_replay(run_id, arguments)
            if identity not in self.inflight and receipt["state"] not in {"completed", "failed"}:
                # Execution can settle while this caller receives its snapshot.
                receipt = await self.inspect(identity)
                if not wait and receipt["state"] != "intent":
                    return receipt
            if receipt["state"] == "completed":
                return json.loads(receipt["result_json"])
            if receipt["state"] == "failed":
                raise RuntimeFault(**json.loads(receipt["error_json"]))
        # A replay can publish the execution before the creator resumes.
        if identity in self.inflight:
            return await self._result(self.inflight[identity], receipt, spec) if wait else receipt
        if receipt["state"] != "intent":
            raise RuntimeFault("OUTCOME_UNCERTAIN", "Inspect the original action before continuing", run_id=run_id, action_id=identity)
        # No await between publishing the task and assigning its identity.
        task = asyncio.create_task(self._execute(receipt, provider, arguments, prepared.bound_arguments), name=identity)
        self.inflight[identity] = task
        self.names[identity] = name
        self.owners[identity] = run_id or intent_id
        def finished(completed):
            if self.inflight.get(identity) is completed:
                self.inflight.pop(identity, None)
            self.owners.pop(identity, None)
            self.names.pop(identity, None)
            self.started.discard(identity)
            self.cancel_requested.discard(identity)
            if not completed.cancelled():
                completed.exception()
            if run_id is not None and self.on_settle:
                self.on_settle(run_id)
            elif intent_id is not None and self.on_intent_settle:
                self.on_intent_settle(intent_id)
        task.add_done_callback(finished)
        return await self._result(task, receipt, spec) if wait else receipt

    def has_inflight(self, run_id):
        return any(owner == run_id and not self.inflight[identity].done() for identity, owner in self.owners.items() if identity in self.inflight)

    def commit_owned(self, db, action_id, run_id, result):
        """Join a local domain mutation and its action receipt atomically."""
        receipt = one(db, "SELECT * FROM actions WHERE id=? AND run_id=?", (action_id, run_id))
        require(receipt is not None and receipt["state"] in {"dispatched", "needs_review", "completed"}, "STALE_RUN", "Action does not own this mutation receipt")
        return self._commit(db, receipt, result)

    def _commit(self, db, receipt, result):
        name, identity, run_id = receipt["name"], receipt["id"], receipt["run_id"]
        result = json.loads(encode(result))
        validate_value(self.validators[name][1], result)
        require(isinstance(result, dict), "TOOL_RESULT_INVALID", "Tool results require a JSON object envelope")
        result = self.redact(result)
        encoded = encode(result)
        db.execute("UPDATE actions SET state='completed',result_json=?,error_json=NULL,updated_at=? WHERE id=? AND state!='completed'", (encoded, now_ms(), identity))
        if not db.changes():
            previous = one(db, "SELECT result_json FROM actions WHERE id=?", (identity,))
            require(previous is not None, "OWNER_DELETED", "The owning work was deleted; its late result was discarded")
            require(previous["result_json"] == encoded, "IDEMPOTENCY_CONFLICT", "An accepted action receipt has a different result")
            return json.loads(previous["result_json"])
        if self.record_progress and run_id is not None and not result.get("isError") and not result.get("truncated"):
            self.record_progress(db, run_id, name, result)
        event(db, "action.completed", run_id=run_id, intent_id=receipt.get("intent_id"), data={"action_id": identity, "isError": bool(result.get("isError"))})
        return result

    async def _result(self, task, receipt, spec):
        if task.done():
            return await task
        remaining = max(0, spec.timeout_seconds - (now_ms() - receipt["created_at"]) / 1000)
        try:
            return await asyncio.wait_for(asyncio.shield(task), remaining)
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                error = await self.store.call(lambda db: one(db, "SELECT error_json FROM actions WHERE id=?", (receipt["id"],)))
                require(error is not None, "OWNER_DELETED", "The owning work was deleted")
                raise RuntimeFault(**json.loads(error["error_json"])) from None
            raise
        except TimeoutError:
            await self._cancel_calls([receipt], reason="timeout")
            if task.done():
                return await task
            raise RuntimeFault("OUTCOME_UNCERTAIN", "Original tool call has not confirmed its outcome or stopped",
                               run_id=receipt["run_id"], call_id=receipt.get("intent_id"), action_id=receipt["id"], details={"reason": "timeout", "cancellation": spec.cancellation}) from None

    async def _execute(self, receipt, provider, arguments, bound_arguments=None):
        identity, run_id, spec = receipt["id"], receipt["run_id"], provider.describe()
        intent_id = receipt.get("intent_id")
        try:
            if run_id is not None:
                await self.authorize(run_id, spec.name)
            else:
                await self.authorize_intent(intent_id, spec.name)
            def dispatch(db):
                self.guard(db, run_id, intent_id)
                db.execute("UPDATE actions SET state='dispatched',updated_at=? WHERE id=? AND state='intent'", (now_ms(), identity))
                require(db.changes() == 1, "STALE_ACTION", "Action is no longer pending dispatch")
            await self.store.call(dispatch, transaction=True)
            self.started.add(identity)
            invocation = asyncio.create_task(provider.invoke({"action_id": identity, "run_id": run_id, "intent_id": intent_id, "bound_arguments": bound_arguments,
                                                              "operation_key": receipt["operation_key"], "arguments": arguments}))
            try:
                result = await invocation
            except asyncio.CancelledError:
                # A managed process can return its actual stopped result while
                # cancellation is being observed. Keep that original evidence.
                if invocation.done() and not invocation.cancelled():
                    result = invocation.result()
                else:
                    raise
            # Function providers normalize typed objects. Generic providers must
            # honor the same JSON boundary before their receipt is committed.
            return await self.store.call(lambda db: self._commit(db, receipt, result), transaction=True)
        except BaseException as error:
            previous = await self.store.call(lambda db: one(db, "SELECT state,result_json,error_json FROM actions WHERE id=?", (identity,)))
            if previous is None:
                raise RuntimeFault("OWNER_DELETED", "The owning work was deleted; its late result was discarded") from error
            if previous["state"] == "completed":
                return json.loads(previous["result_json"])
            if identity not in self.started and isinstance(error, RuntimeFault) and error.code == "STALE_ACTION":
                if previous["state"] == "failed":
                    raise RuntimeFault(**json.loads(previous["error_json"])) from None
                raise RuntimeFault("OUTCOME_UNCERTAIN", "Inspect the original action before continuing", run_id=run_id, action_id=identity) from error
            unknown = identity in self.started and spec.side_effects != "none"
            fault = RuntimeFault("ACTION_NOT_STARTED", "Action stopped before provider execution") if isinstance(error, asyncio.CancelledError) and identity not in self.started else error if isinstance(error, RuntimeFault) and not unknown else RuntimeFault(
                "OUTCOME_UNCERTAIN" if unknown else "TOOL_FAILED", "Tool did not return a valid result",
                details={"cause": error.as_dict() if isinstance(error, RuntimeFault) else type(error).__name__}, run_id=run_id, action_id=identity)
            fault = RuntimeFault(**{**self.redact(fault.as_dict()), "action_id": identity, **({"run_id": run_id} if run_id is not None else {"call_id": intent_id})})
            def fail(db):
                db.execute("UPDATE actions SET state=?,error_json=?,updated_at=? WHERE id=? AND state!='completed'",
                           ("needs_review" if unknown else "failed", encode(fault.as_dict()), now_ms(), identity))
                event(db, "action.needs_review" if unknown else "action.failed", run_id=run_id, intent_id=intent_id,
                      data={"action_id": identity, "error": fault.as_dict()})
            await self.store.call(fail, transaction=True)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise fault from error


    async def confirm(self, action_id, project_id, idempotency_key, evidence, outcome, *, result=None, intent_id=None):
        """Persist external verification once for either real action owner."""
        require(0 < len(idempotency_key) <= 256 and 0 < len(evidence.strip()) <= 8192,
                "INVALID_REQUEST", "Confirmation requires a stable key and concrete verification evidence")
        require((outcome == "completed") == (result is not None), "INVALID_REQUEST", "Supply the actual original result only for a completed action")
        require(outcome in {"completed", "not_applied"}, "INVALID_REQUEST", "Choose a known verification outcome")
        digest = hashlib.sha256(encode({"outcome": outcome, "result": result, "evidence": evidence}).encode("utf-8")).hexdigest()
        def confirm(db):
            action = one(db, "SELECT a.*,COALESCE(i.project_id,t.project_id) AS project_id FROM actions a LEFT JOIN runs r ON r.id=a.run_id LEFT JOIN instances i ON i.id=r.instance_id LEFT JOIN tool_intents t ON t.id=a.intent_id WHERE a.id=?", (action_id,))
            require(action is not None, "NOT_FOUND", "Action does not exist")
            require(action["project_id"] == project_id and (action["run_id"] is not None if intent_id is None else action["intent_id"] == intent_id), "PERMISSION_DENIED", "Action belongs to another owner or project")
            owner = {"run_id": action["run_id"]} if action["run_id"] is not None else {"call_id": action["intent_id"]}
            task = self.inflight.get(action_id)
            require(task is None or task.done(), "ACTION_IN_FLIGHT", "Wait for the original owned invocation before confirming an external outcome")
            previous = one(db, "SELECT * FROM action_resolutions WHERE action_id=?", (action_id,))
            if previous:
                require(previous["idempotency_key"] == idempotency_key and previous["input_hash"] == digest,
                        "IDEMPOTENCY_CONFLICT", "The original action already has a different confirmation")
                return {"action_id": action_id, **owner, "outcome": outcome, "reused": True}
            require(action["state"] == "needs_review" and action["side_effects"] != "none", "ACTION_NOT_CONFIRMABLE", "Confirm only a genuinely unresolved external or local action")
            normalized = None
            if result is not None:
                contract = one(db, "SELECT t.spec_json FROM action_contracts a JOIN tool_contracts t ON t.id=a.contract_id WHERE a.action_id=?", (action_id,))
                require(contract, "CONTRACT_UNAVAILABLE", "The original tool result contract is unavailable")
                normalized = json.loads(encode(self.redact(result)))
                try:
                    validate_value(schema_validator(json.loads(contract["spec_json"])["output_schema"]), normalized)
                except ValidationError:
                    raise RuntimeFault("TOOL_RESULT_INVALID", "Verified result does not match the original action contract") from None
            db.execute("INSERT INTO action_resolutions VALUES(?,?,?,?,?,?,?)", (action_id, idempotency_key, digest, self.redact(evidence), outcome, action["error_json"], now_ms()))
            fault = {"code": "ACTION_CONFIRMED_NOT_APPLIED", "safe_message": "Controller verified that the original effect was not applied", "action_id": action_id, **owner}
            db.execute("UPDATE actions SET state=?,result_json=?,error_json=?,updated_at=? WHERE id=?", (
                "completed" if normalized is not None else "failed", encode(normalized) if normalized is not None else None,
                None if normalized is not None else encode(fault), now_ms(), action_id))
            if action["run_id"] is not None and self.record_progress and normalized is not None and not normalized.get("isError") and normalized.get("exit_code", 0) == 0:
                self.record_progress(db, action["run_id"], action["name"], normalized)
            event(db, "action.confirmed", run_id=action["run_id"], intent_id=action["intent_id"], data={"action_id": action_id, "outcome": outcome})
            return {"action_id": action_id, **owner, "outcome": outcome, "reused": False}
        return await self.store.call(confirm, transaction=True)

    async def inspect(self, action_id):
        receipt = await self.store.call(lambda db: one(db, "SELECT * FROM actions WHERE id=?", (action_id,)))
        require(receipt is not None, "NOT_FOUND", "Action does not exist")
        return receipt

    async def cancel(self, run_id):
        calls = await self.store.call(lambda db: list(db.execute("SELECT * FROM actions WHERE (run_id=? OR intent_id=?) AND state IN ('intent','dispatched','needs_review')", (run_id,run_id))))
        await self._cancel_calls(calls, reason="cancel_requested")

    async def _cancel_calls(self, calls, *, reason):
        pending, fresh = [], []
        for receipt in calls:
            identity = receipt["id"]
            task = self.inflight.get(identity)
            if task is None or task.done() or identity in self.cancel_requested:
                continue
            self.cancel_requested.add(identity)
            fresh.append((receipt, identity in self.started))
            pending.append(task)
            spec = self.providers[receipt["name"]].describe()
            if identity not in self.started or spec.cancellation != "none":
                task.cancel()
        not_started = [receipt for receipt, was_started in fresh if not was_started]
        if not_started:
            def stopped_before_dispatch(db):
                for receipt in not_started:
                    fault = {"code": "ACTION_NOT_STARTED", "safe_message": "Action stopped before provider execution", "action_id": receipt["id"]}
                    db.execute("UPDATE actions SET state='failed',error_json=?,updated_at=? WHERE id=? AND state IN ('intent','dispatched')", (encode(fault), now_ms(), receipt["id"]))
                    if db.changes():
                        event(db, "action.failed", run_id=receipt["run_id"], intent_id=receipt.get("intent_id"), data={"action_id": receipt["id"], "error": fault})
            await self.store.call(stopped_before_dispatch, transaction=True)
        if pending:
            await asyncio.wait(pending, timeout=3)
        for receipt, was_started in fresh:
            identity = receipt["id"]
            task = self.inflight.get(identity)
            if task is None or task.done():
                continue
            error = {"code": "OUTCOME_UNCERTAIN", "safe_message": "Owned call has not confirmed its outcome or stopped",
                     "action_id": identity, "run_id": receipt["run_id"], "call_id": receipt.get("intent_id"), "details": {"reason": reason, "stop_confirmed": False}}
            def pending_fact(db, row=receipt, fault=error):
                db.execute("UPDATE actions SET state='needs_review',error_json=?,updated_at=? WHERE id=? AND state IN ('intent','dispatched')",
                           (encode(fault), now_ms(), row["id"]))
                if db.changes():
                    event(db, "action.needs_review", run_id=row["run_id"], intent_id=row.get("intent_id"), data={"action_id": row["id"], "error": fault})
            await self.store.call(pending_fact, transaction=True)

    async def aclose(self):
        # Actual provider completion is observed and persisted even if a caller
        # left. Closing the owned host cancels and awaits its remaining actions.
        for run_id in tuple(set(self.owners.values())):
            await self.cancel(run_id)
        await asyncio.gather(*tuple(self.inflight.values()), return_exceptions=True)
        self.inflight.clear()
