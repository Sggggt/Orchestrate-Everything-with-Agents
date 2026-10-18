"""Optional provider mediation, durable intents and uncertain side effects."""

import asyncio
import json
from typing import Literal, Protocol

from pydantic import Field

from .errors import RuntimeFault, require
from .models import (
    Model,
    ResourceRequirement,
    TaskSpec,
    content_hash,
    json_dump,
    new_id,
    now_ms,
)
from .store import emit, one


class ToolSpec(Model):
    name: str
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict = Field(default_factory=lambda: {"type": "object"})
    resources: tuple[ResourceRequirement, ...] = ()
    side_effects: Literal["NONE", "LOCAL", "EXTERNAL"] = "NONE"
    timeout_seconds: float = Field(default=60, gt=0, le=3600)
    supports_idempotency: bool = False
    supports_probe: bool = False


class ToolProvider(Protocol):
    def describe(self) -> ToolSpec: ...
    async def invoke(self, request: dict) -> dict: ...
    async def reconcile(self, operation_ref: dict) -> dict: ...


class ToolGateway:
    def __init__(self, runtime):
        self.runtime = runtime
        self.providers = {}
        self.allowed = {}
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

    async def invoke(self, name, arguments, idempotency_key, *, caller_run_id):
        provider = self.providers.get(name)
        require(provider is not None, "NOT_FOUND", "Tool is not registered")
        arguments = self.runtime.clean_json(arguments)
        spec = provider.describe()
        import jsonschema

        try:
            jsonschema.validate(arguments, spec.input_schema)
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
        require(
            task_spec.agent_id in self.allowed[name],
            "PERMISSION_DENIED",
            "Agent is not allowed to invoke this tool",
        )
        digest = content_hash(arguments)
        prior = await self.runtime.store.call(
            lambda db: one(
                db,
                "SELECT * FROM tool_calls WHERE session_id=? AND tool_name=? AND idempotency_key=?",
                (task.session_id, name, idempotency_key),
            )
        )
        if prior:
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
                db.execute(
                    "INSERT INTO tool_calls(id,session_id,run_id,tool_name,idempotency_key,request_hash,state,request_json,resources_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'INTENT',?,?,?,?)",
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

            await self.runtime.store.call(intent, transaction=True)

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
                jsonschema.validate(result, spec.output_schema)

                def complete(db):
                    self.runtime.check_run(db, caller_run_id)
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
                uncertain = spec.side_effects != "NONE"
                state = "UNCERTAIN" if uncertain else "FAILED"
                if not self.runtime.store.failed and not self.runtime.store.closed:
                    await self.runtime.store.call(
                        lambda db: db.execute(
                            "UPDATE tool_calls SET state=?,error_json=?,updated_at=? WHERE id=?",
                            (
                                state,
                                json_dump(
                                    {
                                        "code": (
                                            "OUTCOME_UNCERTAIN"
                                            if uncertain
                                            else "TOOL_FAILED"
                                        )
                                    }
                                ),
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
                    ) from None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeFault("TOOL_FAILED", "Tool invocation failed") from None
        finally:
            self.inflight.discard(caller_run_id)
            if uncertain:
                self.runtime.resources.isolate(
                    owner, "Dispatched tool outcome is uncertain"
                )
            else:
                self.runtime.resources.release(owner)
            self.runtime.notify()

    async def reconcile(self, tool_call_id):
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
