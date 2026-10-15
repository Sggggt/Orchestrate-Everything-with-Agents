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
        self, messages, *, tools=None, include_instructions=True, emit_progress=False
    ):
        request = LLMRequest(
            messages=messages,
            tools=tools,
            include_instructions=include_instructions,
            emit_progress=emit_progress,
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

    async def list_tools(self):
        return await self._call("runtime.tools.list")

    async def list_skills(self):
        return await self._call("runtime.skills.list")

    async def read_skill(self, skill_id, path="SKILL.md"):
        return await self._call("runtime.skills.read", skill_id=skill_id, path=path)

    async def invoke_tool(self, name, arguments, idempotency_key):
        return await self._call(
            "runtime.tools.invoke",
            name=name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )

    async def submit_task(self, spec):
        return await self._call("runtime.tasks.submit", spec=spec)

    async def send_message(self, spec):
        return await self._call("runtime.messages.send", spec=spec)

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

    async def write(self, frame):
        encoded = (json_dump(frame) + "\n").encode("utf-8")
        require(
            len(encoded) <= 1048576,
            "ADAPTER_PROTOCOL_ERROR",
            "JSONL frame exceeds 1 MiB",
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
                    },
                }
            )
            # Parent detects exit instead of receiving a fabricated completion.
            self.exit_requested.set()

    async def run(self):
        self.exit_requested = asyncio.Event()
        try:
            while not self.exit_requested.is_set():
                line = await asyncio.to_thread(sys.stdin.buffer.readline, 1048577)
                if not line:
                    break
                require(
                    len(line) <= 1048576,
                    "ADAPTER_PROTOCOL_ERROR",
                    "JSONL input frame exceeds 1 MiB",
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
                                )
                            )
                        else:
                            future.set_result(frame.get("result"))
                    continue
                method, params = frame["method"], frame.get("params", {})
                if method == "agent.describe":
                    value = self.adapter.describe()
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


def run_agent(adapter):
    """Serve one managed execution. Logs belong on stderr, never stdout."""
    asyncio.run(_Endpoint(adapter).run())
