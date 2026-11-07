"""Durable command sessions. Yielding never restarts or terminates a command."""

import asyncio
import json
import os
import subprocess
import uuid
import codecs
from pathlib import Path

from oea.contracts.errors import RuntimeFault, require
from oea.security.secrets import atomic_write
from oea.stdio.process_tree import OwnedProcessTree
from oea.workspace.paths import safe_child
from oea.storage.persistence import now_ms


class CommandSessions:
    def __init__(self, root, *, on_settle=None):
        self.root = Path(root)
        self.generation = uuid.uuid4().hex
        self.live = {}
        self.by_run = {}
        self.on_settle = on_settle

    def has_live(self, run_id):
        return bool(self.by_run.get(run_id))

    def directory(self, identity):
        return safe_child(self.root, identity)

    def save(self, record):
        atomic_write(self.directory(record["command_session_id"]) / "session.json",
                     json.dumps(record, ensure_ascii=False).encode("utf-8"))

    def load(self, identity, run_id):
        owner = self.live.get(identity)
        if owner is not None:
            require(owner["record"]["run_id"] == run_id, "PERMISSION_DENIED", "Command belongs to another run")
            return owner["record"]
        path = self.directory(identity) / "session.json"
        require(path.is_file(), "COMMAND_NOT_FOUND", "Command session is unavailable")
        record = json.loads(path.read_text(encoding="utf-8"))
        require(record["run_id"] == run_id, "PERMISSION_DENIED", "Command belongs to another run")
        if record["state"] in {"starting", "running"} and identity not in self.live:
            record.update(state="unknown", process_stopped=False)
        return record

    def facts(self, commands):
        """Read lifecycle metadata only; never wait, inspect processes or consume output."""
        result = {}
        for identity, run_id in commands.items():
            try:
                record = self.load(identity, run_id).copy()
            except RuntimeFault as error:
                if error.code != "COMMAND_NOT_FOUND":
                    raise
                continue
            result[identity] = {key: record.get(key) for key in
                ("generation", "state", "exit_code", "process_stopped", "termination", "started_at", "stopped_at")}
            if record.get("error"):
                result[identity]["error"] = {"code": record["error"]["code"]}
        return result

    async def start(self, request, environment):
        args, identity = request["arguments"], request["action_id"]
        require(bool(args.get("argv")) != bool(args.get("command")), "INVALID_REQUEST", "Choose argv or command")
        require(not args.get("tty"), "CAPABILITY_UNAVAILABLE", "This backend supports pipe sessions, not PTY")
        settings, paths = environment.settings, environment.paths
        argv = args.get("argv") or [*environment.shell(), args["command"]]
        cwd = paths.resolve(args.get("cwd", str(paths.work_dir)))
        timeout = min(args.get("timeout_seconds", settings.command_timeout_seconds), settings.command_timeout_seconds)
        directory = self.directory(identity)
        if (directory / "session.json").exists():
            return await self.observe(request["run_id"], {"command_session_id": identity})
        directory.mkdir(parents=True, exist_ok=True)
        record = {"command_session_id": identity, "action_id": identity, "run_id": request["run_id"],
                  "generation": self.generation, "pid": None, "state": "starting", "exit_code": None,
                  "created_at": now_ms(), "started_at": None, "stopped_at": None,
                  "timeout_seconds": timeout,
                  "process_stopped": False, "termination": None, "termination_requested": None, "cwd": str(cwd),
                  "environment": environment.describe(), "stdout_bytes": 0, "stderr_bytes": 0,
                  "retention_bytes": settings.command_output_bytes, "inline_bytes": settings.command_inline_bytes}
        self.save(record)  # Persist intent before launching; unknown launches are never replayed.
        try:
            process, tree = await environment.spawn(argv, cwd, args.get("env"), timeout=timeout)
        except (OSError, RuntimeFault) as error:
            stopped = not isinstance(error, RuntimeFault) or error.code in {"SANDBOX_UNAVAILABLE", "SANDBOX_VERSION_MISMATCH"} or error.details.get("process_stopped") is True
            record.update(state="failed" if stopped else "unknown", process_stopped=stopped,
                          error=error.as_dict() if isinstance(error, RuntimeFault) else {"code": "COMMAND_START_FAILED", "message": str(error)})
            self.save(record)
            return self.result(record, args)
        if getattr(process, "started", None):
            record["environment"]["capabilities"] = process.started["capabilities"]
            record["workload_pid"] = process.started["pid"]
        record.update(state="running", pid=process.pid, started_at=now_ms())
        self.save(record)
        owner = {"process": process, "tree": tree, "record": record, "lock": asyncio.Lock()}
        self.live[identity] = owner
        self.by_run.setdefault(request["run_id"], set()).add(identity)
        owner["task"] = asyncio.create_task(self.monitor(owner, timeout))
        try:
            return await self.observe(request["run_id"], {**args, "command_session_id": identity})
        except asyncio.CancelledError:
            await self.stop(owner, "cancelled")
            return self.result(record, args)

    async def monitor(self, owner, timeout):
        process, tree, record = owner["process"], owner["tree"], owner["record"]
        async def drain(stream, label):
            with (self.directory(record["command_session_id"]) / (label + ".bin")).open("wb", buffering=0) as output:
                while chunk := await stream.read(16384):
                    count = record[label + "_bytes"]
                    output.write(chunk[:max(0, record["retention_bytes"] - count)])
                    record[label + "_bytes"] += len(chunk)
        drains = [asyncio.create_task(drain(process.stdout, "stdout")), asyncio.create_task(drain(process.stderr, "stderr"))]
        try:
            try:
                if getattr(process, "task", None) is not None:
                    await process.wait()  # The native component owns the actual execution deadline.
                else:
                    await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                record["termination"] = "timeout"
                record["termination_requested"] = "timeout"
                await tree.stop()
            # Descendants cannot keep the pipes and run ownership alive indefinitely.
            tree.close()
            await asyncio.gather(*drains)
            record.update(state="exited", exit_code=process.returncode, process_stopped=True, stopped_at=now_ms())
            if getattr(process, "terminal", None):
                observed = process.terminal["termination"]
                record["termination"] = record["termination_requested"] if observed == "cancelled" and record["termination_requested"] else observed
                record["native_termination"] = observed
                record["warnings"] = process.terminal["warnings"]
        except RuntimeFault as error:
            tree.close()
            await asyncio.gather(*drains, return_exceptions=True)
            stopped = error.details.get("process_stopped") is True
            record.update(state="failed" if stopped else "unknown", process_stopped=stopped,
                          exit_code=error.details.get("exit_code"), stopped_at=now_ms() if stopped else None, error=error.as_dict())
        except (OSError, asyncio.CancelledError) as error:
            await tree.stop()
            tree.close()
            await asyncio.gather(*drains, return_exceptions=True)
            record.update(state="failed", exit_code=process.returncode, process_stopped=process.returncode is not None,
                          error={"code": "COMMAND_CAPTURE_FAILED", "message": str(error)})
        finally:
            tree.close()
            self.save(record)
            self.live.pop(record["command_session_id"], None)
            self.by_run[record["run_id"]].discard(record["command_session_id"])
            if not self.by_run[record["run_id"]]:
                self.by_run.pop(record["run_id"])
            if self.on_settle:
                await self.on_settle(record)

    async def stop(self, owner, reason):
        owner["record"]["termination_requested"] = reason
        if getattr(owner["process"], "task", None) is None and owner["process"].returncode is None:
            owner["record"]["termination"] = reason
        await owner["tree"].stop()
        await asyncio.shield(owner["task"])

    async def observe(self, run_id, args, environment=None):
        identity = args["command_session_id"]
        record = self.load(identity, run_id)
        owner = self.live.get(identity)
        mutating = bool(args.get("chars")) or args.get("close_stdin") or args.get("terminate")
        require(not mutating or owner is not None, "COMMAND_NOT_RUNNING", "Cannot write to or terminate an unowned command")
        if mutating and not args.get("terminate") and environment is not None and record["environment"]["revision"] != environment.describe()["revision"]:
            await self.stop(owner, "permission_changed")
            require(False, "PERMISSION_DENIED", "The command environment changed; the old command was stopped")
        if owner is not None:
            record = owner["record"]
            if args.get("terminate"):
                await self.stop(owner, "cancelled")
            else:
                async with owner["lock"]:
                    stream = owner["process"].stdin
                    if args.get("chars"):
                        require(not stream.is_closing(), "STDIN_CLOSED", "Command input is closed")
                        try:
                            stream.write(args["chars"].encode("utf-8"))
                            await stream.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            require(False, "STDIN_CLOSED", "Command closed its input")
                    if args.get("close_stdin"):
                        stream.close()
            if not owner["task"].done():
                await asyncio.wait({owner["task"]}, timeout=args.get("yield_time_ms", 1000) / 1000)
        return self.result(record, args)

    def result(self, record, args):
        result = dict(record)
        result["isError"] = record["state"] in {"failed", "unknown"} or record["exit_code"] not in {None, 0} or record["termination"] is not None
        references = []
        for label in ("stdout", "stderr"):
            path = self.directory(record["command_session_id"]) / (label + ".bin")
            offset = args.get(label + "_offset", 0)
            data = b""
            saved_bytes, available = 0, False
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                pass
            else:
                with stream:
                    available = True
                    stream.seek(offset)
                    data = stream.read(record["inline_bytes"])
                    saved_bytes = os.fstat(stream.fileno()).st_size
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            final = offset + len(data) >= saved_bytes and (record["state"] not in {"starting", "running"} or saved_bytes >= record["retention_bytes"])
            result[label] = decoder.decode(data, final=final)
            result[label + "_next_offset"] = offset + len(data) - len(decoder.getstate()[0])
            references.append({"stream": label, "command_session_id": record["command_session_id"],
                               "saved_bytes": saved_bytes, "available": available,
                               "observed_bytes": None if record["state"] == "unknown" else record[label + "_bytes"],
                               "truncated": None if record["state"] == "unknown" else record[label + "_bytes"] > saved_bytes})
        result["output_refs"] = references
        return result

    async def stop_run(self, run_id, reason="run_finished"):
        await asyncio.gather(*(self.stop(self.live[identity], reason) for identity in tuple(self.by_run.get(run_id, ()))))

    async def close(self):
        await asyncio.gather(*(self.stop(owner, "host_closed") for owner in tuple(self.live.values())))
