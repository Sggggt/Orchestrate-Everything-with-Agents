"""Author a v2 process Agent with ``run_agent(program)``."""

import asyncio
import json
import sys

from oea.contracts.domain import AgentDescriptor
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode
from oea.stdio.protocol import MAX_FRAME_BYTES, ProgramResult


class ProcessContext:
    def __init__(self, endpoint, request):
        self._endpoint, self.request = endpoint, request
        self.run_id, self.instance_id = request["run_id"], request["instance_id"]
        self.input, self.paths = request["input"], request["paths"]
        self.checkpoint, self.reply = request["checkpoint"], request["reply"]
        self.cancelled = asyncio.Event()

    async def invoke_tool(self, name, arguments, idempotency_key):
        return await self._endpoint.call("tools.invoke", {"name": name, "arguments": arguments, "idempotency_key": idempotency_key})

    async def list_tools(self):
        return await self._endpoint.call("tools.list", {})

    async def instructions(self):
        return await self._endpoint.call("instructions.get", {})

    async def complete(self, idempotency_key):
        """Call the saved model using the host's bounded Context projection."""
        return await self._endpoint.call("model.complete", {"idempotency_key": idempotency_key})

    async def execute_model_calls(self, response_id):
        return await self._endpoint.call("tools.execute_model_calls", {"response_id": response_id})

    async def progress(self, text):
        require(len(text) <= 8192, "INVALID_REQUEST", "Use bounded progress text")
        await self._endpoint.write({"method": "agent.progress", "params": {"text": text}})


class _Endpoint:
    def __init__(self, program):
        self.program, self.pending, self.sequence = program, {}, 0
        self.writing = asyncio.Lock()
        self.worker = self.context = None

    async def write(self, frame):
        raw = (encode({"jsonrpc": "2.0", **frame}) + "\n").encode("utf-8")
        require(len(raw) <= MAX_FRAME_BYTES, "PROCESS_PROTOCOL_ERROR", "JSONL frame exceeds 16 MiB")
        async with self.writing:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    async def call(self, method, params):
        self.sequence += 1
        identity = "program:" + str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.write({"id": identity, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(identity, None)

    async def execute(self, request):
        self.context = ProcessContext(self, request)
        try:
            result = ProgramResult.model_validate(await self.program.execute(self.context))
            descriptor = AgentDescriptor.model_validate(self.program.describe())
            require(result.status != "suspended" or descriptor.supports_suspend, "PROCESS_PROTOCOL_ERROR", "Program did not declare suspension support")
            await self.call("run.boundary", {"result": result.model_dump(mode="json")})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            fault = error if isinstance(error, RuntimeFault) else RuntimeFault("PROGRAM_FAILED", "Process Agent failed before a terminal boundary", details={"cause": type(error).__name__})
            await self.write({"method": "agent.failed", "params": {"error": fault.as_dict()}})
            print("Agent execution failed (" + type(error).__name__ + ")", file=sys.stderr, flush=True)
            raise

    async def serve(self):
        reading = None
        try:
            while True:
                # Inherited Windows stdin is a synchronous anonymous pipe,
                # which Proactor connect_read_pipe cannot attach to IOCP.
                # A single blocking reader has no polling or buffer growth.
                reading = asyncio.create_task(asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME_BYTES + 1))
                done, _ = await asyncio.wait({reading, self.worker} if self.worker else {reading}, return_when=asyncio.FIRST_COMPLETED)
                if self.worker in done:
                    reading.cancel()
                    await asyncio.gather(reading, return_exceptions=True)
                    await self.worker
                    return
                raw = reading.result()
                if not raw:
                    require(self.worker is None or self.worker.done(), "PROCESS_PROTOCOL_ERROR", "Host disconnected before a terminal boundary")
                    return
                require(len(raw) <= MAX_FRAME_BYTES, "PROCESS_PROTOCOL_ERROR", "JSONL frame exceeds 16 MiB")
                frame = json.loads(raw.decode("utf-8"))
                require(frame.get("jsonrpc") == "2.0", "PROCESS_PROTOCOL_ERROR", "Use JSON-RPC 2.0 over UTF-8 JSONL")
                method, identity = frame.get("method"), frame.get("id")
                if method == "agent.describe":
                    await self.write({"id": identity, "result": AgentDescriptor.model_validate(self.program.describe()).model_dump(mode="json")})
                elif method == "agent.execute":
                    require(self.worker is None, "PROCESS_PROTOCOL_ERROR", "One process executes one Run turn")
                    self.worker = asyncio.create_task(self.execute(frame["params"]))
                elif method == "agent.cancel":
                    if self.context:
                        self.context.cancelled.set()
                elif identity in self.pending:
                    if "error" in frame:
                        self.pending[identity].set_exception(RuntimeFault(**frame["error"]))
                    else:
                        self.pending[identity].set_result(frame["result"])
                else:
                    raise RuntimeFault("PROCESS_PROTOCOL_ERROR", "Unsupported host frame")
        finally:
            if reading and not reading.done():
                reading.cancel()
            if self.worker and not self.worker.done():
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)


def run_agent(program):
    """Run one process turn; exceptions produce a nonzero process exit."""
    asyncio.run(_Endpoint(program).serve())


__all__ = ["run_agent", "ProcessContext", "ProgramResult", "AgentDescriptor"]
