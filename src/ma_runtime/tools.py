"""Optional provider mediation, durable intents and uncertain side effects."""

import asyncio
import json

from .errors import RuntimeFault, require
from .models import (
    TaskSpec,
    content_hash,
    json_dump,
    new_id,
    now_ms,
)
from .store import emit, one
from .tool_contracts import ToolSpec, ToolProvider


class ToolGateway:
    def __init__(self, runtime):
        self.runtime = runtime
        self.providers = {}
        self.allowed = {}
        self.validators = {}
        self.inflight = set()

    def register(self, provider, *, allowed_agent_ids):
        spec = provider.describe()
        require(
            spec.name not in self.providers,
            "INVALID_REQUEST",
            "Tool name is already registered",
        )
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(spec.input_schema)
        jsonschema.Draft202012Validator.check_schema(spec.output_schema)
        self.providers[spec.name] = provider
        self.allowed[spec.name] = frozenset(allowed_agent_ids)
        self.validators[spec.name] = tuple(jsonschema.validators.validator_for(schema)(schema)
            for schema in (spec.input_schema, spec.output_schema))

    async def invoke(self, name, arguments, idempotency_key, *, caller_run_id, parent_stage_id=None):
        provider = self.providers.get(name)
        require(provider is not None, "NOT_FOUND", "Tool is not registered")
        arguments = self.runtime.clean_json(arguments)
        spec = provider.describe()
        import jsonschema

        try:
            self.validators[name][0].validate(arguments)
        except jsonschema.ValidationError:
            raise RuntimeFault(
                "INVALID_REQUEST", "Tool input does not match its schema"
            ) from None
        task, run = await self.runtime.store.call(
            lambda db: self.runtime.check_run(db, caller_run_id)
        )
        task_spec = TaskSpec.model_validate_json(task.spec_json)
        from .execution_policy import mcp_name, record
        identifier = mcp_name(provider.server_id, provider.tool["name"]) if hasattr(provider, "server_id") else name
        require(task_spec.tool_policy.permits(identifier), "PERMISSION_DENIED", "Tool is disabled by this task's policy")
        from .models import WORKBENCH_TOOLS
        if name in WORKBENCH_TOOLS:
            active = self.runtime.active.get(caller_run_id)
            require(active is not None, "STALE_RUN", "Run is no longer active")
            self.runtime.workbench.authorize(active.request, name)
        else:
            require(task_spec.agent_id in self.allowed[name], "PERMISSION_DENIED", "Agent is not allowed to invoke this tool")
        digest = content_hash(arguments)
        prior = await self.runtime.store.call(
            lambda db: one(
                db,
                "SELECT * FROM tool_calls WHERE session_id=? AND tool_name=? AND idempotency_key=?",
                (task.session_id, name, idempotency_key),
            )
        )
        if prior:
            previous_run = await self.runtime.store.call(lambda db: one(db, "SELECT task_id FROM runs WHERE id=?", (prior.run_id,)))
            require(previous_run.task_id == task.id, "PERMISSION_DENIED", "Tool receipt belongs to another task")
            require(
                prior.request_hash == digest,
                "IDEMPOTENCY_CONFLICT",
                "Tool key has different input",
            )
            if prior.state == "SUCCEEDED":
                result = json.loads(prior.result_json)
                if not result.get("isError") and (task_spec.tool_policy.required or task_spec.tool_policy.mode == "required"):
                    def reuse(db):
                        self.runtime.check_run(db, caller_run_id)
                        record(db, task.id, run.id, "tool", identifier)
                    await self.runtime.store.call(reuse, transaction=True)
                return result
            raise RuntimeFault(
                (
                    "OUTCOME_UNCERTAIN"
                    if prior.state in {"DISPATCHED", "UNCERTAIN"}
                    else "TOOL_ALREADY_ATTEMPTED"
                ),
                "Existing tool invocation must be queried or reconciled",
            )
        active = self.runtime.active.get(caller_run_id)
        self.runtime.continuations.check_action(active.context if active else None, name, arguments, spec.side_effects)
        held = {r.key: r.mode for r in task_spec.resources}
        if held:
            require(
                all(
                    r.key in held and (held[r.key] == "EXCLUSIVE" or r.mode == "SHARED")
                    for r in spec.resources
                ),
                "RESOURCE_SET_INCOMPLETE",
                "Tool resources must be covered by task resources",
            )
        require(
            caller_run_id not in self.inflight,
            "RESOURCE_SET_INCOMPLETE",
            "Nested or concurrent tool calls in a run are not permitted",
        )
        identity = new_id("tool")
        owner = caller_run_id + ":" + identity
        require(
            bool(held) or self.runtime.resources.acquire(owner, spec.resources),
            "RESOURCE_BUSY",
            "Tool resource is busy",
            retryable=True,
        )
        self.inflight.add(caller_run_id)
        uncertain = False
        try:

            def intent(db):
                self.runtime.check_run(db, caller_run_id)
                self.runtime.budgets.tool(db, active.request, identity, identifier)
                db.execute(
                    "INSERT INTO tool_calls(id,session_id,run_id,tool_name,idempotency_key,request_hash,state,request_json,resources_json,created_at,updated_at,parent_stage_id) VALUES(?,?,?,?,?,?,'INTENT',?,?,?,?,?)",
                    (
                        identity,
                        task.session_id,
                        caller_run_id,
                        name,
                        idempotency_key,
                        digest,
                        json_dump(arguments),
                        json_dump(spec.resources),
                        now_ms(),
                        now_ms(),
                        parent_stage_id,
                    ),
                )
                emit(
                    db,
                    "tool.intent",
                    session_id=task.session_id,
                    thread_id=task.thread_id,
                    task_id=task.id,
                    run_id=caller_run_id,
                    payload={"tool_call_id": identity, "tool_name": name},
                )

            try:
                await self.runtime.store.call(intent, transaction=True)
            except RuntimeFault as exc:
                self.runtime.budgets.reject(active.context, exc)
                raise

            def dispatch(db):
                self.runtime.check_run(db, caller_run_id)
                db.execute(
                    "UPDATE tool_calls SET state='DISPATCHED',updated_at=? WHERE id=?",
                    (now_ms(), identity),
                )

            await self.runtime.store.call(dispatch, transaction=True)
            request = {
                "tool_call_id": identity,
                "caller_run_id": caller_run_id,
                "session_id": task.session_id,
                "thread_id": task.thread_id,
                "arguments": arguments,
                "idempotency_key": (
                    idempotency_key if spec.supports_idempotency else None
                ),
            }
            try:
                result = await asyncio.wait_for(
                    provider.invoke(request), spec.timeout_seconds
                )
                result = self.runtime.clean_json(result)
                self.validators[name][1].validate(result)

                def complete(db):
                    self.runtime.check_run(db, caller_run_id)
                    if not result.get("isError") and not result.get("truncated") and identifier != "publish_artifact":
                        # MCP receipt IDs and transport metadata do not make a
                        # repeated business result new evidence.
                        evidence = {key: result[key] for key in ("content", "structuredContent") if key in result} if identifier.startswith("mcp:") else result
                        self.runtime.budgets.progress(db, task.id, identifier, evidence)
                    if not result.get("isError") and (task_spec.tool_policy.required or task_spec.tool_policy.mode == "required"):
                        record(db, task.id, run.id, "tool", identifier)
                    db.execute(
                        "UPDATE tool_calls SET state='SUCCEEDED',result_json=?,updated_at=? WHERE id=?",
                        (json_dump(result), now_ms(), identity),
                    )
                    emit(
                        db,
                        "tool.completed",
                        session_id=task.session_id,
                        thread_id=task.thread_id,
                        task_id=task.id,
                        run_id=caller_run_id,
                        payload={"tool_call_id": identity, "tool_name": name},
                    )

                await self.runtime.store.call(complete, transaction=True)
                return result
            except BaseException as exc:
                not_dispatched = isinstance(exc, RuntimeFault) and exc.details.get("dispatched") is False
                uncertain = spec.side_effects != "NONE" and not not_dispatched
                state = "UNCERTAIN" if uncertain else "FAILED"
                cause = exc.as_dict() if isinstance(exc, RuntimeFault) else {"code": "TOOL_FAILED", "exception_type": type(exc).__name__}
                error = self.runtime.vault.redact({"code": "OUTCOME_UNCERTAIN", "category": "tool", "retryable": False,
                    "safe_message": "Dispatched tool action requires reconciliation",
                    "details": {"stage": "tool", "tool_call_id": identity, "dispatched": True, "cause": cause}}
                    if uncertain else cause)
                if not self.runtime.store.failed and not self.runtime.store.closed:
                    await self.runtime.store.call(
                        lambda db: db.execute(
                            "UPDATE tool_calls SET state=?,error_json=?,updated_at=? WHERE id=?",
                            (
                                state,
                                json_dump(error),
                                now_ms(),
                                identity,
                            ),
                        ),
                        transaction=True,
                    )
                if uncertain:
                    raise RuntimeFault(
                        "OUTCOME_UNCERTAIN",
                        "Dispatched tool action requires reconciliation",
                        details=error["details"],
                    ) from None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, RuntimeFault):
                    raise
                raise RuntimeFault("TOOL_FAILED", "Tool invocation failed") from None
        finally:
            self.inflight.discard(caller_run_id)
            if uncertain:
                self.runtime.resources.isolate(
                    owner, "Dispatched tool outcome is uncertain"
                )
                if name.startswith("mcp_") and not self.runtime.store.closed:
                    call = await self.runtime.store.call(lambda db: one(db,
                        "SELECT id,result_json FROM mcp_calls WHERE run_id=? AND idempotency_key=?",
                        (caller_run_id, idempotency_key)))
                    if call:
                        cached = self.runtime.mcp.result_cache.get(call.id)
                        result = json.loads(call.result_json) if call.result_json else cached[0] if cached else None
                        if result is not None:
                            await self.observe_mcp_response(call.id, result)
            else:
                self.runtime.resources.release(owner)
            self.runtime.notify()

    async def observe_mcp_response(self, call_id, result):
        """Settle a mediated receipt from its verified original MCP response, without replay."""
        from .mcp_service import tool_alias
        result = self.runtime.mcp.bounded_result(result, self.runtime.config.max_message_bytes - 2048)
        def observe(db):
            upstream = one(db, "SELECT c.*,r.state AS resource_state FROM mcp_calls c "
                           "JOIN mcp_call_resources r ON r.call_id=c.id WHERE c.id=?", (call_id,))
            if upstream is None or not upstream.run_id or upstream.resource_state != "RELEASED" or upstream.state not in {"SUCCEEDED", "FAILED"}:
                return None
            call = one(db, "SELECT * FROM tool_calls WHERE run_id=? AND idempotency_key=? AND tool_name=?",
                       (upstream.run_id, upstream.idempotency_key, tool_alias(upstream.server_id, upstream.tool_name)))
            if call is None or call.state not in {"UNCERTAIN", "FAILED", "SUCCEEDED"}:
                return None
            if call.state != "SUCCEEDED":
                db.execute("UPDATE tool_calls SET state='SUCCEEDED',result_json=?,error_json=NULL,updated_at=? WHERE id=?",
                           (json_dump(result), now_ms(), call.id))
                emit(db, "tool.observed", run_id=call.run_id, payload={"tool_call_id": call.id, "mcp_call_id": call_id})
            return call.run_id + ":" + call.id
        owner = await self.runtime.store.call(observe, transaction=True)
        if owner is not None:
            self.runtime.resources.resolve(owner)
        return owner is not None

    async def reconcile(self, tool_call_id, resolution=None):
        from .services import need

        call = await self.runtime.store.call(
            lambda db: need(db, "tool_calls", tool_call_id)
        )
        provider = self.providers.get(call.tool_name)
        require(
            provider and provider.describe().supports_probe,
            "UNSUPPORTED_CAPABILITY",
            "Tool cannot query external outcomes",
        )
        require(
            call.state == "UNCERTAIN", "INVALID_STATE", "Tool call is not uncertain"
        )
        if resolution is not None:
            from .models import Reconciliation
            resolution = Reconciliation.model_validate(resolution)
            require(resolution.confirmed_stopped and resolution.external_actions_confirmed
                    and resolution.resolution != "QUEUED" and call.run_id not in self.inflight,
                    "OUTCOME_UNCERTAIN", "Verify execution stop and effects before resolving a tool; reconciliation never replays it")
            probe = {"confirmed": True, "evidence": resolution.evidence,
                     "succeeded": resolution.resolution == "SUCCEEDED",
                     "result": resolution.result.output if resolution.result else {}}
        else:
            probe = await provider.reconcile(
                {
                    "tool_call_id": call.id,
                    "idempotency_key": call.idempotency_key,
                    "operation_ref": (
                        json.loads(call.external_operation_ref_json)
                        if call.external_operation_ref_json
                        else None
                    ),
                }
            )
        require(
            probe.get("confirmed") and probe.get("evidence"),
            "OUTCOME_UNCERTAIN",
            "Tool probe did not establish a verifiable outcome",
        )
        await self.runtime.store.call(
            lambda db: db.execute(
                "UPDATE tool_calls SET state=?,result_json=?,error_json=?,updated_at=? WHERE id=?",
                (
                    "SUCCEEDED" if probe.get("succeeded") else "RESOLVED",
                    json_dump(probe.get("result", {})),
                    json_dump({"evidence": probe["evidence"]}),
                    now_ms(),
                    call.id,
                ),
            ),
            transaction=True,
        )
        self.runtime.resources.resolve(call.run_id + ":" + call.id)
        return {"resolved": True, "evidence": probe["evidence"]}
