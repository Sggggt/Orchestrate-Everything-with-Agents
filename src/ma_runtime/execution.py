"""Explicit local environments and owned command lifecycles, without a sandbox claim."""

import asyncio
import json
import os
from pathlib import Path
import subprocess

from .errors import RuntimeFault, require
from .models import (AdapterEvent, AgentConfig, AgentDescriptor, CancelReceipt,
                     EnvironmentConfig, ProbeResult, TaskResult, TaskSpec, json_dump, now_ms, new_id)
from .store import one, rows


class Environments:
    def __init__(self, runtime):
        self.rt = runtime
        self.versions = {}

    async def open(self):
        entries = await self.rt.store.call(lambda db: rows(db,
            "SELECT * FROM environment_versions ORDER BY revision"))
        for entry in entries:
            self.versions[entry.id] = {"revision": entry.revision,
                "config": EnvironmentConfig.model_validate_json(entry.config_json)}

    async def list(self, project_id=None):
        return [{"id": identity, "revision": entry["revision"],
                 "project_id": entry["config"].project_id,
                 "runner": entry["config"].runner, "enabled": entry["config"].enabled}
                for identity, entry in self.versions.items()
                if project_id is None or entry["config"].project_id == project_id]

    async def get(self, environment_id):
        entry = self.entry(environment_id)
        return {"revision": entry["revision"], **entry["config"].model_dump(mode="json")}

    def entry(self, identity):
        require(identity in self.versions, "NOT_FOUND", "Environment is not configured")
        return self.versions[identity]

    async def configure(self, config: EnvironmentConfig | dict):
        self.rt.admit()
        config = EnvironmentConfig.model_validate(config)
        await self.rt.projects.get(config.project_id)
        require(all(key.replace("_", "").isalnum() and not key.upper().endswith(("TOKEN", "KEY", "SECRET", "PASSWORD"))
                    for key in config.variables), "INVALID_REQUEST", "Use nonsecret environment variables")
        for name, program in config.programs.items():
            require(name and not name.startswith("-") and Path(program).is_absolute(),
                    "INVALID_REQUEST", "Program aliases must identify explicit absolute paths")
        for guest, source in config.resources.items():
            require(guest.startswith("/resources/") and ".." not in guest.split("/"),
                    "INVALID_REQUEST", "Resources must be mapped below /resources")
            require(Path(source).is_absolute(), "INVALID_REQUEST", "Resource directories must be absolute")
            path = Path(source).expanduser().resolve()
            require(path.is_dir() and not path.is_relative_to(self.rt.root) and not self.rt.root.is_relative_to(path),
                    "PERMISSION_DENIED", "Resources cannot expose platform state")
        async with self.rt.configuration_lock:
            previous = self.versions.get(config.id)
            require(previous is None or previous["config"].project_id == config.project_id,
                    "PROJECT_MISMATCH", "Environment project cannot change")
            revision = previous["revision"] + 1 if previous else 1
            await self.rt.store.call(lambda db: db.execute("INSERT INTO environment_versions VALUES(?,?,?,?)",
                (config.id, revision, json_dump(config), now_ms())), transaction=True)
            self.versions[config.id] = {"revision": revision, "config": config}
        return await self.get(config.id)

    def validate(self, agent, *, require_host=True):
        if agent.workbench:
            env = self.entry(agent.workbench.environment_id)["config"]
            require(env.enabled and env.project_id == agent.project_id,
                    "PERMISSION_DENIED", "Environment is disabled or belongs to another project")
            require(not require_host or agent.workbench.permission_profile != "trusted-local" or self.rt.allow_trusted_local,
                    "PERMISSION_DENIED", "The host must explicitly allow trusted-local execution")

    def snapshot(self, db, task_id, agent, project):
        if not agent or not agent.workbench:
            return None
        saved = one(db, "SELECT snapshot_json FROM task_workbenches WHERE task_id=?", (task_id,))
        if saved:
            return json.loads(saved.snapshot_json)
        self.validate(agent, require_host=False)
        entry = self.entry(agent.workbench.environment_id)
        value = {**agent.workbench.model_dump(mode="json"), "revision": entry["revision"],
                 "environment": entry["config"].model_dump(mode="json"),
                 "project_folder": project.local_folder,
                 "directory": str(self.rt.root / "workbench" / task_id)}
        db.execute("INSERT INTO task_workbenches VALUES(?,?)", (task_id, json_dump(value)))
        return value

    def check_delegation(self, db, parent_run_id, child_agent):
        parent, _ = self.rt.check_run(db, parent_run_id)
        child = self.rt.agent_configs.get(child_agent)
        if not child or not child.workbench:
            return
        saved = one(db, "SELECT snapshot_json FROM task_workbenches WHERE task_id=?", (parent.id,))
        parent_scope = json.loads(saved.snapshot_json) if saved else None
        ranks = {"read-only": 0, "workspace-write": 1, "trusted-local": 2}
        require(parent_scope and child.workbench.environment_id == parent_scope["environment_id"]
                and set(child.workbench.tools) <= set(parent_scope["tools"])
                and ranks[child.workbench.permission_profile] <= ranks[parent_scope["permission_profile"]],
                "PERMISSION_DENIED", "Child workbench would expand the parent's permission")

    async def _check(self, environment_id, revision):
        row = await self.rt.store.call(lambda db: one(db,
            "SELECT config_json FROM environment_versions WHERE id=? AND revision=?", (environment_id, revision)))
        config = EnvironmentConfig.model_validate_json(row.config_json)
        require(config.enabled, "ENVIRONMENT_UNAVAILABLE", "Environment is disabled")
        directory = self.rt.root / "workbench"
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        missing = [name for name, path in config.programs.items() if not Path(path).is_file()]
        local = config.runner == "local" and config.network == "inherit"
        return {"environment_id": environment_id, "revision": revision, "runner": config.runner,
                "base_ready": True, "task_ready": not missing and local,
                "execution_ready": local and self.rt.allow_trusted_local and not missing,
                "missing_programs": missing,
                "guarantees": {"file_isolation": False, "network_isolation": False, "owned_process_tree": local},
                "limitations": [] if local else ["This host has no requested isolation backend"]}

    async def prepare(self, environment_id, idempotency_key):
        import hashlib
        env = self.entry(environment_id)["config"]
        agent_id = "oea-env-" + hashlib.sha256(env.project_id.encode()).hexdigest()[:16]
        if agent_id not in self.rt.agent_configs:
            await self.rt.agents.configure(AgentConfig(id=agent_id, name="Environment preparation",
                project_id=env.project_id, kind="environment", collaboration=False, max_concurrency=1))
        revision = self.entry(environment_id)["revision"]
        require(isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 256,
                "INVALID_REQUEST", "Use a stable preparation key")
        def create(db):
            from .services import resolve_project
            from .store import emit
            resolve_project(db, env.project_id)
            prior = one(db, "SELECT * FROM environment_jobs WHERE environment_id=? AND idempotency_key=?",
                        (environment_id, idempotency_key))
            if prior:
                require(prior.revision == revision, "IDEMPOTENCY_CONFLICT", "Preparation key has another environment revision")
                return {"task_id": prior.task_id, "session_id": prior.session_id, "reused": True}
            session_id, thread_id = new_id("ses"), new_id("thr")
            db.execute("INSERT INTO sessions(id,state,title,metadata_json,created_at,project_id) VALUES(?,'OPEN',?,'{}',?,?)",
                (session_id, "Environment preparation", now_ms(), env.project_id))
            db.execute("INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,created_at) VALUES(?,?,?,?,?,?)",
                (thread_id, session_id, agent_id, "Environment preparation", json_dump(self.rt.adapters[agent_id].describe()), now_ms()))
            task = self.rt.tasks.insert(db, TaskSpec(session_id=session_id, thread_id=thread_id,
                agent_id=agent_id, payload={"environment_id": environment_id, "revision": revision}, idempotency_key=idempotency_key))
            db.execute("INSERT INTO environment_jobs VALUES(?,?,?,?,?)", (environment_id, idempotency_key, revision, task.id, session_id))
            emit(db, "session.created", session_id=session_id, payload={"title": "Environment preparation"})
            return {"task_id": task.id, "session_id": session_id, "reused": False}
        result = await self.rt.store.call(create, transaction=True)
        self.rt.notify()
        return result

    async def check(self, environment_id, idempotency_key):
        return await self.prepare(environment_id, idempotency_key)


class EnvironmentAgent:
    def __init__(self, agent_id, environments):
        self.agent_id, self.environments = agent_id, environments
        self.running = {}

    def describe(self):
        return AgentDescriptor(agent_id=self.agent_id, max_concurrency=1)

    async def execute(self, request, context):
        env = self.environments.entry(request.payload["environment_id"])["config"]
        require(env.project_id == request.project_id, "PERMISSION_DENIED", "Environment belongs to another project")
        job = asyncio.create_task(self.environments._check(env.id, request.payload["revision"]))
        self.running[request.run_id] = job
        try:
            yield AdapterEvent(type="completed", result=TaskResult(output=await job))
        finally:
            self.running.pop(request.run_id, None)

    async def cancel(self, run_ref):
        job = self.running.get(run_ref.get("run_id"))
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        return CancelReceipt(accepted=True, confirmed_stopped=True)

    async def probe(self, run_ref):
        active = run_ref.get("run_id") in self.running
        return ProbeResult(state="RUNNING" if active else "STOPPED", confirmed_stopped=not active)

    async def aclose(self):
        for job in self.running.values():
            job.cancel()


async def execute(workspace, request):
    from agent.process_tree import OwnedProcessTree
    env, args = workspace.scope["environment"], request["arguments"]
    executable = env["programs"].get(args["argv"][0])
    if executable is None and args["argv"][0] in env["programs"].values():
        executable = args["argv"][0]
    require(executable and Path(executable).is_file(), "DEPENDENCY_MISSING", "Select a configured, available program alias")
    cwd = workspace.path(args.get("cwd", "/workspace"), directory=True)
    temporary = workspace.directory / "tmp"
    temporary.mkdir(exist_ok=True)
    variables = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    variables.update({"PATH": os.pathsep.join(dict.fromkeys(str(Path(path).parent) for path in env["programs"].values())),
        "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "HOME": str(workspace.directory), "USERPROFILE": str(workspace.directory),
        "OEA_WORKSPACE": str(workspace.directory), "PYTHONDONTWRITEBYTECODE": "1", **env["variables"]})
    arguments = [str(workspace.path(arg)) if arg.startswith(("/workspace/", "/project/", "/resources/")) else arg
                 for arg in args["argv"][1:]]
    process = await asyncio.create_subprocess_exec(executable, *arguments, cwd=cwd, env=variables,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
    try:
        tree = OwnedProcessTree(process)
    except BaseException:
        process.terminate()
        await process.wait()
        raise
    logs = []
    async def drain(stream, label):
        output = bytearray()
        path = workspace.directory / (request["tool_call_id"] + "." + label)
        count = 0
        with path.open("wb") as log:
            while chunk := await stream.read(8192):
                output.extend(chunk[:max(0, env["output_bytes"] - len(output))])
                if count < 4 * 1024 * 1024:
                    log.write(chunk[:4 * 1024 * 1024 - count])
                count += len(chunk)
        logs.append((label, path, count))
        return output.decode("utf-8", errors="replace")
    stdout = asyncio.create_task(drain(process.stdout, "stdout"))
    stderr = asyncio.create_task(drain(process.stderr, "stderr"))
    timeout = min(args.get("timeout_seconds", env["timeout_seconds"]), env["timeout_seconds"])
    active = workspace.runtime.active[request["caller_run_id"]]
    cancellation = asyncio.create_task(active.context.cancelled.wait())
    stopped = asyncio.create_task(process.wait())
    try:
        await workspace.runtime.store.call(lambda db: db.execute(
            "UPDATE tool_calls SET external_operation_ref_json=? WHERE id=?",
            (json_dump({"pid": process.pid, "host_instance_id": workspace.runtime.host_instance_id}), request["tool_call_id"])),
            transaction=True)
        done, _ = await asyncio.wait({stopped, cancellation}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done:
            raise asyncio.CancelledError
        if not done:
            raise TimeoutError
        await stopped
    except BaseException:
        await tree.stop()
        tree.close()
        await asyncio.gather(stdout, stderr)
        raise
    finally:
        tree.close()
        cancellation.cancel()
        await asyncio.gather(cancellation, stopped, return_exceptions=True)
    result = {"exit_code": process.returncode, "stdout": await stdout, "stderr": await stderr,
              "isError": process.returncode != 0, "pid": process.pid, "artifacts": [],
              "isolation": "trusted-local: host user permissions, no filesystem or network isolation"}
    for label, path, count in logs:
        if count > env["output_bytes"]:
            artifact = await workspace.runtime.artifacts.put_file(workspace.session_id, path,
                caller_run_id=request["caller_run_id"])
            result["artifacts"].append({"stream": label, "artifact_id": artifact.id,
                "bytes": count, "truncated": count > 4 * 1024 * 1024})
    return result
