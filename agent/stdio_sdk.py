"""Run an AgentAdapter over the platform's managed JSONL execution protocol.

Usage: ``run_agent(MyAdapter())``. The platform owns one child per attempt,
streams events, and closes the child after accepting its terminal boundary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid

from ma_runtime.errors import RuntimeFault, require
from ma_runtime.models import MAX_AGENT_FRAME_BYTES
from ma_runtime.models import (
    AdapterEvent,
    ExecutionRequest,
    LLMRequest,
    LLMResponse,
    json_dump,
)


class RemoteLLM:
    """Model calls are executed by the host using the Agent's saved API settings."""

    def __init__(self, context):
        self.context = context

    async def info(self):
        return await self.context._call("runtime.llm.info")

    async def complete(
        self, messages, *, tools=None, include_instructions=True, emit_progress=False, tool_choice=None,
        allow_incomplete=False
    ):
        request = LLMRequest(
            messages=messages,
            tools=tools,
            include_instructions=include_instructions,
            emit_progress=emit_progress,
            tool_choice=tool_choice,
            allow_incomplete=allow_incomplete,
        )
        result = await self.context._call(
            "runtime.llm.complete", request=request.model_dump(mode="json")
        )
        return LLMResponse.model_validate(result)


class RemoteRunContext:
    def __init__(self, endpoint, request):
        self.endpoint, self.request = endpoint, request
        self.cancelled = asyncio.Event()
        self.llm = RemoteLLM(self)

    async def _call(self, method, **params):
        return await self.endpoint.rpc(
            method, {**params, "run_id": self.request.run_id}
        )

    async def list_threads(self):
        return await self._call("runtime.threads.list")

    async def report_issue(self, summary, idempotency_key, details="", wait=False):
        return await self._call("runtime.operations.report", summary=summary, idempotency_key=idempotency_key, details=details, wait=wait)

    async def operations(self, action, params=None):
        return await self._call("runtime.operations.call", action=action, params=params or {})

    async def list_tools(self):
        return await self._call("runtime.tools.list")

    async def read_context(self, reference, offset=0, limit=8192):
        return await self._call("runtime.context.read", reference=reference, offset=offset, limit=limit)

    async def project_context(self, value):
        return await self._call("runtime.context.project", value=value)

    async def list_skills(self):
        return await self._call("runtime.skills.list")

    async def read_skill(self, skill_id, path="SKILL.md"):
        return await self._call("runtime.skills.read", skill_id=skill_id, path=path)

    async def invoke_tool(self, name, arguments, idempotency_key, *, model_call_id=None, tool_call_id=None):
        return await self._call(
            "runtime.tools.invoke",
            name=name,
            arguments=arguments,
            idempotency_key=idempotency_key,
            model_call_id=model_call_id, tool_call_id=tool_call_id,
        )

    async def submit_task(self, spec):
        return await self._call("runtime.tasks.submit", spec=spec)

    async def spawn_instances(self, spec):
        return await self._call("runtime.instances.spawn", spec=spec)

    async def send_message(self, spec):
        return await self._call("runtime.messages.send", spec=spec)

    async def reject_message(self, message_id, reason, idempotency_key):
        return await self._call("runtime.messages.reject", message_id=message_id, reason=reason, idempotency_key=idempotency_key)

    async def inbox(self, after_position=0, limit=32):
        return await self._call("runtime.channel.inbox", after_position=after_position, limit=limit)

    async def acknowledge_messages(self, positions):
        return await self._call("runtime.channel.ack", positions=positions)

    async def reconnect_channel(self, connection_id):
        previous = self.endpoint.channel or {}
        require("reconnect" in previous.get("capabilities", ()), "UNSUPPORTED_CAPABILITY", "Host does not support channel reconnection")
        value = await self._call("runtime.channel.reconnect", connection_id=connection_id,
                                 expected_generation=previous["generation"])
        self.endpoint.channel = value
        return value

    async def set_run_ref(self, native_ref):
        return await self._call("runtime.run_ref.set", ref=native_ref)

    async def put_bytes(self, name, data, *, metadata=None):
        require(
            isinstance(data, bytes) and len(data) <= 524288,
            "ARTIFACT_INVALID",
            "JSONL SDK artifacts are limited to 512 KiB per frame",
        )
        return await self._call(
            "runtime.artifacts.put_bytes",
            name=name,
            base64=base64.b64encode(data).decode("ascii"),
            metadata=metadata,
        )


class _Endpoint:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pending = {}
        self.writing = asyncio.Lock()
        self.worker = None
        self.context = None
        self.channel = None

    async def write(self, frame):
        if frame.get("id") == "host:describe" and "result" in frame:
            frame = {**frame, "extensions": {"oea.channel": "1.0"}}
        if self.channel and (frame.get("method") == "agent.event" or frame.get("method", "").startswith("runtime.")):
            frame = {**frame, "params": {**frame.get("params", {}), "channel": self.channel}}
        encoded = (json_dump(frame) + "\n").encode("utf-8")
        require(
            len(encoded) <= MAX_AGENT_FRAME_BYTES,
            "ADAPTER_PROTOCOL_ERROR",
            "JSONL frame exceeds 16 MiB",
        )
        async with self.writing:
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()

    async def rpc(self, method, params):
        identity = "agent:" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.write(
                {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}
            )
            return await future
        finally:
            self.pending.pop(identity, None)

    async def execute(self, request):
        self.context = RemoteRunContext(self, request)
        seq = 0
        try:
            async for raw in self.adapter.execute(request, self.context):
                event = AdapterEvent.model_validate(raw)
                seq += 1
                params = {
                    "run_id": request.run_id,
                    "local_seq": seq,
                    "event": event.model_dump(mode="json"),
                }
                if event.type in {"completed", "suspended"}:
                    await self.rpc("runtime.commit_boundary", params)
                    return
                await self.write(
                    {"jsonrpc": "2.0", "method": "agent.event", "params": params}
                )
            raise RuntimeFault(
                "ADAPTER_PROTOCOL_ERROR", "Agent ended without a terminal boundary"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never write stack traces, arbitrary tool data or credentials to stdout.
            print(
                f"Agent execution failed ({type(exc).__name__})",
                file=sys.stderr,
                flush=True,
            )
            failure = exc if isinstance(exc, RuntimeFault) else RuntimeFault(
                "ADAPTER_FAILED", "Agent execution failed before a boundary",
                details={"exception_type": type(exc).__name__[:120]},
            )
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "method": "agent.event",
                    "params": {
                        "run_id": request.run_id,
                        "local_seq": seq + 1,
                        "event": {
                            "type": "log",
                            "payload": {
                                "error": "Agent execution ended without a boundary"
                            },
                        },
                    },
                }
            )
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "id": "host:execute",
                    "error": {
                        "code": -32000,
                        "message": "Agent execution ended without a boundary",
                        "data": failure.as_dict(),
                    },
                }
            )
            # Parent detects exit instead of receiving a fabricated completion.
            self.exit_requested.set()

    async def run(self):
        self.exit_requested = asyncio.Event()
        try:
            while not self.exit_requested.is_set():
                line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_AGENT_FRAME_BYTES + 1)
                if not line:
                    break
                require(
                    len(line) <= MAX_AGENT_FRAME_BYTES,
                    "ADAPTER_PROTOCOL_ERROR",
                    "JSONL input frame exceeds 16 MiB",
                )
                frame = json.loads(line)
                identity = frame.get("id")
                if "method" not in frame:
                    future = self.pending.get(identity)
                    if future and not future.done():
                        if "error" in frame:
                            error = frame["error"].get("data", frame["error"])
                            future.set_exception(
                                RuntimeFault(
                                    error.get("code", "REMOTE_ERROR"),
                                    error.get(
                                        "safe_message", "Platform callback failed"
                                    ),
                                    retryable=error.get("retryable", False),
                                    details=error.get("details", {}),
                                )
                            )
                        else:
                            future.set_result(frame.get("result"))
                    continue
                method, params = frame["method"], frame.get("params", {})
                if method == "agent.describe":
                    value = self.adapter.describe()
                elif method == "agent.channel":
                    require(self.worker is None and params.get("protocol_version") == "1.0", "UNSUPPORTED_CAPABILITY", "Cannot bind this execution channel")
                    self.channel = params
                    value = {"generation": params["generation"]}
                elif method == "agent.execute":
                    require(
                        self.worker is None,
                        "ADAPTER_PROTOCOL_ERROR",
                        "Only one execution is allowed per child",
                    )
                    request = ExecutionRequest.model_validate(params)
                    await self.write(
                        {"jsonrpc": "2.0", "id": identity, "result": {"started": True}}
                    )
                    self.worker = asyncio.create_task(self.execute(request))
                    continue
                elif method == "agent.cancel":
                    if self.context:
                        self.context.cancelled.set()
                    value = await self.adapter.cancel(params)
                elif method == "agent.probe":
                    value = await self.adapter.probe(params)
                elif method == "agent.fork_checkpoint":
                    try:
                        require(self.worker is None and hasattr(self.adapter, "fork_checkpoint"),
                                "UNSUPPORTED_CAPABILITY", "This child cannot fork checkpoints")
                        value = await self.adapter.fork_checkpoint(params["reference"], params["thread_id"])
                    except RuntimeFault as exc:
                        await self.write({"jsonrpc": "2.0", "id": identity,
                            "error": {"code": -32000, "message": exc.safe_message, "data": exc.as_dict()}})
                        continue
                else:
                    await self.write(
                        {
                            "jsonrpc": "2.0",
                            "id": identity,
                            "error": {
                                "code": -32601,
                                "message": "Unsupported Agent method",
                            },
                        }
                    )
                    continue
                await self.write({"jsonrpc": "2.0", "id": identity, "result": value})
        finally:
            if self.worker and not self.worker.done():
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            await self.adapter.aclose()


async def run_agent_async(adapter):
    """Serve within an existing event loop, including an owned async checkpointer."""
    await _Endpoint(adapter).run()


def run_agent(adapter):
    """Serve one managed execution. Logs belong on stderr, never stdout."""
    asyncio.run(run_agent_async(adapter))
