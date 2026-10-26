"""One owned process turn with shared Run capabilities and durable boundaries."""

import asyncio
import json
import os
import subprocess

from oea.contracts.domain import AgentDescriptor
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, one
from oea.stdio.protocol import MAX_FRAME_BYTES, ProgramResult
from oea.stdio.process_tree import OwnedProcessTree
from oea.tools.contracts import ToolSpec

PROGRAM_TOOL = "_oea_program"


class ProcessAgentTool:
    def __init__(self, host, *, enabled):
        self.host, self.enabled = host, enabled
        self.spec = ToolSpec(name=PROGRAM_TOOL, description="Execute one explicitly configured process turn", side_effects="local", cancellation="owned_process", timeout_seconds=3600)

    def describe(self):
        return self.spec

    async def invoke(self, request):
        context = self.host.active[request["run_id"]]
        definition, paths = context.definition, context.paths
        settings = definition.workspace
        temporary = paths.work_dir / "tmp"
        temporary.mkdir(exist_ok=True)
        environment = {**os.environ, **settings.environment, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
            "OEA_PROJECT_DIR": str(paths.project_dir or ""), "OEA_WORK_DIR": str(paths.work_dir), "OEA_OUTPUT_DIR": str(paths.output_dir)}
        if (definition.model.credential_ref or "").startswith("env:"):
            environment.pop(definition.model.credential_ref[4:], None)
        process = await asyncio.create_subprocess_exec(*definition.execution.program, cwd=paths.work_dir, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME_BYTES,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
        try:
            tree = OwnedProcessTree(process)
        except BaseException:
            process.terminate()
            await process.wait()
            raise
        log_path = paths.output_dir / (request["action_id"] + ".stderr.txt")

        async def stderr():
            count = 0
            with log_path.open("wb") as output:
                while chunk := await process.stderr.read(16384):
                    output.write(chunk[:max(0, 16 * 1024 * 1024 - count)])
                    count += len(chunk)

        logging = asyncio.create_task(stderr())
        writing, calls, identities = asyncio.Lock(), set(), set()
        terminal, descriptor = None, None

        async def send(frame):
            raw = (encode({"jsonrpc": "2.0", **frame}) + "\n").encode("utf-8")
            require(len(raw) <= MAX_FRAME_BYTES, "PROCESS_PROTOCOL_ERROR", "JSONL frame exceeds 16 MiB")
            async with writing:
                process.stdin.write(raw)
                await process.stdin.drain()

        async def receive():
            raw = await process.stdout.readline()
            require(raw and len(raw) <= MAX_FRAME_BYTES, "PROCESS_PROTOCOL_ERROR", "Process ended before its terminal boundary or exceeded the frame bound")
            try:
                frame = json.loads(raw.decode("utf-8"))
                require(isinstance(frame, dict) and frame.get("jsonrpc") == "2.0", "PROCESS_PROTOCOL_ERROR", "Use UTF-8 JSON-RPC 2.0 frames")
                return frame
            except (ValueError, UnicodeError):
                raise RuntimeFault("PROCESS_PROTOCOL_ERROR", "Process stdout must contain UTF-8 JSONL frames") from None

        async def dispatch(frame):
            nonlocal terminal
            identity, method, params = frame["id"], frame["method"], frame.get("params", {})
            try:
                if method == "tools.list":
                    result = self.host.tools.catalog(context.tool_names())
                elif method == "instructions.get":
                    result = {"text": await context.instructions()}
                elif method == "tools.invoke":
                    key = params.get("idempotency_key")
                    require(isinstance(key, str) and 0 < len(key) <= 256 and params.get("name") in context.tool_names(), "PERMISSION_DENIED", "Use a bound tool and a stable operation key")
                    result = await self.host.tools.invoke(context.run["id"], params["name"], params["arguments"], context.run["id"] + ":process-tool:" + key)
                elif method == "model.complete":
                    require(definition.execution.program_uses_model, "PERMISSION_DENIED", "This program did not declare model use")
                    key = params.get("idempotency_key")
                    require(isinstance(key, str) and 0 < len(key) <= 256, "INVALID_REQUEST", "Model calls need a stable key")
                    action_id, response = await self.host.models.complete(context, context.run["id"] + ":process-model:" + key, context.model_input)
                    result = {"response_id": action_id, **response}
                elif method == "tools.execute_model_calls":
                    response = await context.response(params["response_id"])
                    result = await context.execute_tools({"response_id": params["response_id"], "action_keys": [context.run["id"] + ":tool:" + call["id"] for call in response["message"].get("tool_calls", [])]})
                elif method == "run.boundary":
                    result = ProgramResult.model_validate(params["result"]).model_dump(mode="json")
                    require(result["status"] != "suspended" or descriptor.supports_suspend, "PROCESS_PROTOCOL_ERROR", "Suspension was not declared")
                    require(not any(key != request["action_id"] and owner == context.run["id"] for key, owner in self.host.tools.owners.items()), "OUTCOME_UNCERTAIN", "A terminal boundary cannot hide an unresolved tool call")
                    result.update(descriptor=descriptor.model_dump(mode="json"))
                    # The accepted boundary is durable before the child sees its ACK.
                    await self.host.store.call(lambda db: self.host.tools.commit_owned(db, request["action_id"], context.run["id"], result), transaction=True)
                    terminal = result
                    result = {"accepted": True, "action_id": request["action_id"]}
                else:
                    raise RuntimeFault("METHOD_NOT_FOUND", "Process capability is unavailable")
                await send({"id": identity, "result": result})
            except Exception as error:
                fault = error if isinstance(error, RuntimeFault) else RuntimeFault("INVALID_REQUEST" if isinstance(error, (KeyError, ValueError, TypeError)) else "PROCESS_CAPABILITY_FAILED",
                    "Process capability call failed", details={"cause": type(error).__name__})
                await send({"id": identity, "error": self.host.redact(fault.as_dict())})

        try:
            async with asyncio.timeout(definition.execution.program_timeout_seconds):
                await send({"id": "host:describe", "method": "agent.describe", "params": {"protocol_version": "2.0"}})
                frame = await receive()
                require(frame.get("id") == "host:describe" and isinstance(frame.get("result"), dict), "PROCESS_PROTOCOL_ERROR", "Process must answer describe before execution")
                require(frame["result"].get("protocol_version") == "2.0", "PROTOCOL_VERSION_UNSUPPORTED", "Use the v2 process SDK; migrate old programs before execution")
                descriptor = AgentDescriptor.model_validate(frame["result"])
                require(descriptor.agent_id == definition.id, "PROCESS_IDENTITY_MISMATCH", "Program descriptor does not match its configured Agent")
                require(not descriptor.supports_probe, "UNSUPPORTED_CAPABILITY", "The v2 process transport does not implement probe")
                previous = await self.host.tools.inspect(request["arguments"]["checkpoint_ref"]) if request["arguments"].get("checkpoint_ref") else None
                checkpoint = json.loads(previous["result_json"])["checkpoint"] if previous else {}
                if checkpoint:
                    prior = json.loads(previous["result_json"])["descriptor"]
                    require(descriptor.checkpoint_format and descriptor.checkpoint_format == prior["checkpoint_format"], "CHECKPOINT_FORMAT_CHANGED", "Program checkpoint format differs from the suspended boundary")
                async def source_input(record, field, fallback):
                    if record and record.get("source_message_id"):
                        source = await self.host.store.call(lambda db: one(db, "SELECT body FROM messages WHERE id=?", (record["source_message_id"],)))
                        return source["body"]
                    return json.loads(record[field]) if record else fallback
                reply = await source_input(context.resumption, "input_json", None)
                goal = await source_input(context.run, "input_json", None)
                await send({"method": "agent.execute", "params": {"run_id": context.run["id"], "instance_id": context.instance["id"],
                    "input": goal, "paths": paths.as_dict(), "checkpoint": checkpoint, "reply": reply}})
                while terminal is None:
                    frame = await receive()
                    if frame.get("method") == "agent.failed":
                        raise RuntimeFault("PROGRAM_FAILED", "Process reported failure before a terminal boundary", details={"error": frame.get("params", {}).get("error")})
                    if frame.get("method") == "agent.progress":
                        text = frame.get("params", {}).get("text", "")
                        require(isinstance(text, str) and len(text) <= 8192, "PROCESS_PROTOCOL_ERROR", "Use bounded progress text")
                        await self.host.emit_progress(context.run["id"], {"kind": "program.progress", "text": self.host.redact(text)})
                        continue
                    identity = frame.get("id")
                    require(isinstance(identity, str) and identity not in identities and isinstance(frame.get("method"), str), "PROCESS_PROTOCOL_ERROR", "Process calls need unique request identities")
                    identities.add(identity)
                    task = asyncio.create_task(dispatch(frame))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
                    # Boundary processing is short and must complete before
                    # waiting for another frame from a child waiting for its ACK.
                    if frame["method"] == "run.boundary":
                        await task
                # EOF releases the SDK's one blocking stdin reader after ACK.
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 3)
                return terminal
        except (asyncio.CancelledError, TimeoutError) as error:
            if descriptor and descriptor.supports_cancel and process.returncode is None:
                await send({"method": "agent.cancel", "params": {}})
            await tree.stop()
            return {"isError": True, "process_stopped": True, "exit_code": process.returncode,
                "termination": "cancelled" if isinstance(error, asyncio.CancelledError) else "timeout"}
        except Exception as error:
            await tree.stop()
            fault = error if isinstance(error, RuntimeFault) else RuntimeFault("PROGRAM_FAILED", "Process failed before its terminal boundary", details={"cause": type(error).__name__})
            return {"isError": True, "process_stopped": True, "exit_code": process.returncode, "error": self.host.redact(fault.as_dict())}
        finally:
            if process.returncode is None:
                await tree.stop()
            tree.close()
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, logging, return_exceptions=True)
            await self.host.store.call(lambda db: event(db, "program.stopped", run_id=context.run["id"],
                data={"action_id": request["action_id"], "pid": process.pid, "exit_code": process.returncode, "boundary_accepted": terminal is not None}), transaction=True)
