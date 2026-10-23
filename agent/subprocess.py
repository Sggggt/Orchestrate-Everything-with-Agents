"""One owned JSONL child per execution attempt, with scoped reverse RPC."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from contextlib import nullcontext

from ma_runtime.errors import RuntimeFault, require
from ma_runtime.models import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    LLMRequest,
    ProbeResult,
    json_dump,
    new_id,
    MAX_AGENT_FRAME_BYTES,
)

from .process_tree import OwnedProcessTree


class JsonlAgent:
    def __init__(self, agent_id, command, root, *, max_concurrency=1, descriptor=None):
        self.agent_id, self.command, self.root = agent_id, tuple(command), root
        self.descriptor = descriptor or AgentDescriptor(
            agent_id=agent_id,
            execution_mode="subprocess",
            max_concurrency=max_concurrency,
            cancellation="terminate_owned_process",
            tool_mediation="partial",
        )
        self.running = {}
        self.finished = {}
        self.controls = {}
        self.max_frame = MAX_AGENT_FRAME_BYTES

    def describe(self):
        return self.descriptor

    async def _send(self, process, frame):
        data = json_dump(frame).encode() + b"\n"
        require(
            len(data) <= self.max_frame,
            "ADAPTER_PROTOCOL_ERROR",
            "Subprocess frame exceeds the configured limit",
        )
        process.stdin.write(data)
        await process.stdin.drain()

    async def _read(self, process):
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise RuntimeFault(
                "ADAPTER_PROTOCOL_ERROR", "Oversized subprocess frame"
            ) from None
        require(
            line,
            "ADAPTER_PROTOCOL_ERROR",
            "Subprocess exited before its commit boundary",
        )
        require(
            len(line) <= self.max_frame,
            "ADAPTER_PROTOCOL_ERROR",
            "Oversized subprocess frame",
        )
        try:
            frame = json.loads(line)
        except (ValueError, UnicodeError):
            raise RuntimeFault(
                "ADAPTER_PROTOCOL_ERROR", "Subprocess stdout is not valid UTF-8 JSONL"
            ) from None
        require(
            isinstance(frame, dict) and frame.get("jsonrpc") == "2.0",
            "ADAPTER_PROTOCOL_ERROR",
            "Invalid subprocess RPC envelope",
        )
        return frame

    async def _drain_stderr(self, stream):
        while await stream.read(8192):
            pass  # Raw provider output is deliberately not logged.

    async def execute(self, request, context):
        require(
            self.command, "AGENT_NOT_CONFIGURED", "Configure a subprocess command first"
        )
        kwargs = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.root,
            limit=self.max_frame + 1,
            **kwargs,
        )
        try:
            tree = OwnedProcessTree(process)
        except BaseException:
            process.terminate()
            await process.wait()
            raise
        self.running[request.run_id] = (process, tree)
        stderr = asyncio.create_task(self._drain_stderr(process.stderr))
        channel = None
        try:
            ref = {
                "run_id": request.run_id,
                "pid": process.pid,
                "host_instance_id": context._runtime.host_instance_id,
            }
            await context.set_run_ref(ref)
            await self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": "host:describe",
                    "method": "agent.describe",
                    "params": {},
                },
            )
            description = await self._read(process)
            require(
                description.get("id") == "host:describe" and "result" in description,
                "ADAPTER_PROTOCOL_ERROR",
                "Subprocess did not negotiate capabilities",
            )
            declared = AgentDescriptor.model_validate(description["result"])
            require(
                declared.agent_id == self.agent_id
                and declared.protocol_version.split(".")[0] == "1"
                and declared.nested_execution != "unmanaged",
                "UNSUPPORTED_CAPABILITY",
                "Subprocess capabilities do not satisfy this host",
            )
            for feature in ("supports_suspend", "supports_fork"):
                require(
                    not getattr(self.descriptor, feature) or getattr(declared, feature),
                    "UNSUPPORTED_CAPABILITY",
                    "Subprocess lacks configured capability",
                )
            if description.get("extensions", {}).get("oea.channel") == "1.0":
                channel = await context._runtime.channels.bind(request.run_id, request.thread_id,
                    new_id("connection"), capabilities=("generation", "inbox", "receipts", "reconnect"))
                await self._send(process, {"jsonrpc": "2.0", "id": "host:channel", "method": "agent.channel", "params": channel})
                acknowledgement = await self._read(process)
                require(acknowledgement.get("id") == "host:channel" and acknowledgement.get("result", {}).get("generation") == channel["generation"],
                        "ADAPTER_PROTOCOL_ERROR", "Subprocess did not acknowledge its channel generation")
            await self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": "host:execute",
                    "method": "agent.execute",
                    "params": request.model_dump(mode="json"),
                },
            )
            last_seq, started = 0, False
            while True:
                frame = await self._read(process)
                if frame.get("id") == "host:cancel" and (
                    "result" in frame or "error" in frame
                ):
                    continue
                if frame.get("id") == "host:execute" and "method" not in frame:
                    error = frame.get("error")
                    failure = error.get("data") if isinstance(error, dict) else None
                    if isinstance(failure, dict) and isinstance(failure.get("code"), str) and isinstance(failure.get("safe_message"), str):
                        raise RuntimeFault(failure["code"], failure["safe_message"],
                            retryable=failure.get("retryable", False), details=failure.get("details", {}))
                    require(
                        "result" in frame and frame["result"].get("started"),
                        "ADAPTER_PROTOCOL_ERROR",
                        "Subprocess rejected execute",
                    )
                    started = True
                    continue
                require(
                    started,
                    "ADAPTER_PROTOCOL_ERROR",
                    "Subprocess must acknowledge execution before callbacks",
                )
                method, params = frame.get("method"), frame.get("params", {})
                require(
                    isinstance(params, dict)
                    and params.get("run_id", request.run_id) == request.run_id,
                    "PERMISSION_DENIED",
                    "Subprocess callback has an invalid run identity",
                )
                identity = frame.get("id")
                if method != "agent.event":
                    require(isinstance(identity, (str, int)) and not isinstance(identity, bool)
                            and not str(identity).startswith("host:"), "ADAPTER_PROTOCOL_ERROR",
                            "Reverse RPC requires a non-host request ID")
                if channel is not None:
                    supplied = params.get("channel", {})
                    require(isinstance(supplied, dict) and supplied.get("run_id") == request.run_id and supplied.get("thread_id") == request.thread_id,
                            "PERMISSION_DENIED", "Subprocess channel belongs to another execution")
                    if method == "runtime.channel.reconnect":
                        try:
                            channel = await context._runtime.channels.bind(request.run_id, request.thread_id,
                                params.get("connection_id"), capabilities=channel["capabilities"],
                                expected_generation=params.get("expected_generation"))
                            await self._send(process, {"jsonrpc": "2.0", "id": identity, "result": channel})
                        except RuntimeFault as exc:
                            await self._send(process, {"jsonrpc": "2.0", "id": identity,
                                "error": {"code": -32000, "message": exc.safe_message, "data": exc.as_dict()}})
                        continue
                    if supplied.get("generation") != channel["generation"]:
                        if method != "agent.event":
                            fault = RuntimeFault("STALE_CHANNEL", "This connection no longer owns the logical channel")
                            await self._send(process, {"jsonrpc": "2.0", "id": identity,
                                "error": {"code": -32000, "message": fault.safe_message, "data": fault.as_dict()}})
                        continue
                with context._runtime.channels.use(params.get("channel")) if channel else nullcontext():
                    if method == "agent.event":
                        local_seq = int(params["local_seq"])
                        require(
                            local_seq > last_seq,
                            "ADAPTER_PROTOCOL_ERROR",
                            "Subprocess event sequence did not increase",
                        )
                        last_seq = local_seq
                        event = AdapterEvent.model_validate(params["event"])
                        require(
                            event.type in {"progress", "usage", "log"},
                            "ADAPTER_PROTOCOL_ERROR",
                            "Final boundaries require commit RPC",
                        )
                        yield event.model_copy(update={"local_seq": local_seq})
                        continue
                    try:
                        if method == "runtime.commit_boundary":
                            seq = int(params["local_seq"])
                            require(
                                seq > last_seq,
                                "ADAPTER_PROTOCOL_ERROR",
                                "Boundary sequence did not increase",
                            )
                            event = AdapterEvent.model_validate(params["event"]).model_copy(
                                update={"local_seq": seq}
                            )
                            value = await context.commit_boundary(event, seq)
                            await self._send(
                                process, {"jsonrpc": "2.0", "id": identity, "result": value}
                            )
                            self.finished[request.run_id] = event
                            yield event
                            return
                        if method == "runtime.tasks.submit":
                            value = await context.submit_task(params["spec"])
                        elif method == "runtime.instances.spawn":
                            value = await context.spawn_instances(params["spec"])
                        elif method == "runtime.llm.info":
                            value = await context.llm.info()
                        elif method == "runtime.llm.complete":
                            from pydantic import ValidationError

                            try:
                                model_request = LLMRequest.model_validate(params["request"])
                            except (ValidationError, KeyError):
                                raise RuntimeFault(
                                    "INVALID_REQUEST", "Invalid model request"
                                ) from None
                            value = await context.llm.complete(
                                model_request.messages,
                                tools=model_request.tools,
                                include_instructions=model_request.include_instructions,
                                allow_incomplete=model_request.allow_incomplete,
                                emit_progress=model_request.emit_progress,
                                tool_choice=model_request.tool_choice,
                            )
                        elif method == "runtime.operations.report":
                            value = await context.report_issue(params["summary"], params["idempotency_key"],
                                details=params.get("details", ""), wait=params.get("wait", False))
                        elif method == "runtime.operations.call":
                            value = await context.operations(params["action"], params.get("params"))
                        elif method == "runtime.tools.list":
                            value = await context.list_tools()
                        elif method == "runtime.context.read":
                            value = await context.read_context(params["reference"], params.get("offset", 0), params.get("limit", 8192))
                        elif method == "runtime.context.project":
                            value = await context.project_context(params["value"])
                        elif method == "runtime.result.retain_partial":
                            value = await context.retain_partial_result(params["result"])
                        elif method == "runtime.skills.list":
                            value = await context.list_skills()
                        elif method == "runtime.skills.read":
                            value = await context.read_skill(
                                params["skill_id"], params.get("path", "SKILL.md")
                            )
                        elif method == "runtime.threads.list":
                            value = await context.list_threads()
                        elif method == "runtime.run_ref.set":
                            await context.set_run_ref(params["ref"])
                            value = {"saved": True}
                        elif method == "runtime.artifacts.put_bytes":
                            try:
                                data = base64.b64decode(params["base64"], validate=True)
                            except (ValueError, TypeError):
                                raise RuntimeFault(
                                    "INVALID_REQUEST", "Artifact content must be base64"
                                ) from None
                            value = await context.put_bytes(
                                params["name"], data, metadata=params.get("metadata")
                            )
                        elif method == "runtime.channel.inbox":
                            require(channel is not None, "UNSUPPORTED_CAPABILITY", "This child did not negotiate channel support")
                            value = await context._runtime.channels.inbox(channel, after_position=params.get("after_position", 0), limit=params.get("limit", 32))
                        elif method == "runtime.channel.ack":
                            require(channel is not None, "UNSUPPORTED_CAPABILITY", "This child did not negotiate channel support")
                            value = await context._runtime.channels.acknowledge(channel, params["positions"])
                        elif method == "runtime.messages.reject":
                            value = await context.reject_message(params["message_id"], params["reason"], params["idempotency_key"])
                        elif method == "runtime.messages.send":
                            value = await context.send_message(params["spec"])
                        elif method == "runtime.tools.invoke":
                            value = await context.invoke_tool(
                                params["name"],
                                params["arguments"],
                                params["idempotency_key"],
                                model_call_id=params.get("model_call_id"), tool_call_id=params.get("tool_call_id"),
                            )
                        else:
                            raise RuntimeFault(
                                "PERMISSION_DENIED",
                                "Subprocess callback is not in its granted scope",
                            )
                        await self._send(
                            process, {"jsonrpc": "2.0", "id": identity, "result": value}
                        )
                    except RuntimeFault as exc:
                        await self._send(
                            process,
                            {
                                "jsonrpc": "2.0",
                                "id": identity,
                                "error": {
                                    "code": -32000,
                                    "message": exc.safe_message,
                                    "data": exc.as_dict(),
                                },
                            },
                        )
        finally:
            await tree.stop()
            tree.close()
            await stderr
            self.running.pop(request.run_id, None)
            if channel is not None:
                await context._runtime.channels.disconnect(channel)

    def discard_committed_boundary(self, run_id):
        self.finished.pop(run_id, None)

    async def cancel(self, run_ref):
        item = self.running.get(run_ref.get("run_id"))
        if item:
            process, tree = item
            try:
                await self._send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": "host:cancel",
                        "method": "agent.cancel",
                        "params": run_ref,
                    },
                )
            except (OSError, RuntimeFault):
                pass
            await tree.stop()
        return CancelReceipt(
            accepted=True,
            confirmed_stopped=item is None or item[0].returncode is not None,
        )

    async def probe(self, run_ref):
        identity = run_ref.get("run_id")
        if identity in self.running:
            process, _ = self.running[identity]
            return ProbeResult(
                state="RUNNING" if process.returncode is None else "STOPPED",
                confirmed_stopped=process.returncode is not None,
            )
        if identity in self.finished:
            return ProbeResult(
                state="COMPLETED",
                confirmed_stopped=True,
                boundary=self.finished[identity],
                evidence="Owned child submitted a committed boundary",
            )
        # A new host cannot infer process identity from a recycled PID.
        if run_ref.get("pid"):
            return ProbeResult(
                state="UNKNOWN",
                evidence="Previous child identity requires explicit reconciliation",
            )
        return ProbeResult(
            state="STOPPED",
            confirmed_stopped=True,
            evidence="No child is owned by this adapter",
        )

    async def fork_checkpoint(self, reference, thread_id):
        require(self.descriptor.supports_fork and self.descriptor.checkpoint_mode == "native_ref",
                "UNSUPPORTED_CAPABILITY", "This process Agent cannot fork native checkpoints")
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=self.root, limit=self.max_frame + 1, **kwargs)
        try:
            tree = OwnedProcessTree(process)
        except BaseException:
            process.terminate()
            await process.wait()
            raise
        self.controls[process.pid] = tree
        stderr = asyncio.create_task(self._drain_stderr(process.stderr))
        try:
            async with asyncio.timeout(10):
                await self._send(process, {"jsonrpc": "2.0", "id": "host:describe", "method": "agent.describe", "params": {}})
                description = await self._read(process)
                declared = AgentDescriptor.model_validate(description.get("result", {}))
                require(declared.agent_id == self.agent_id and declared.supports_fork and declared.checkpoint_mode == "native_ref"
                        and declared.checkpoint_format_version == self.descriptor.checkpoint_format_version,
                        "UNSUPPORTED_CAPABILITY", "Child does not implement the configured checkpoint fork")
                await self._send(process, {"jsonrpc": "2.0", "id": "host:fork", "method": "agent.fork_checkpoint",
                    "params": {"reference": reference, "thread_id": thread_id}})
                reply = await self._read(process)
                require(reply.get("id") == "host:fork", "ADAPTER_PROTOCOL_ERROR", "Invalid fork reply")
                if "error" in reply:
                    error = reply["error"].get("data", {})
                    raise RuntimeFault(error.get("code", "FORK_FAILED"), error.get("safe_message", "Child checkpoint fork failed"))
                require(isinstance(reply.get("result"), dict), "ADAPTER_PROTOCOL_ERROR", "Fork reply must contain a checkpoint reference")
                return reply["result"]
        except TimeoutError:
            raise RuntimeFault("FORK_UNCERTAIN", "Checkpoint fork did not return; retry only with the same fork key") from None
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                await tree.stop()
            tree.close()
            self.controls.pop(process.pid, None)
            await stderr

    async def aclose(self):
        for _, tree in tuple(self.running.values()):
            await tree.stop()
        for tree in tuple(self.controls.values()):
            await tree.stop()
