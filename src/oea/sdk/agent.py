"""Lazy standalone Agents and instances using the shared local or remote host."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import hashlib
import re
from pathlib import Path

from oea.contracts.configuration import AgentDefinition, Instructions, Model, Workspace
from oea.contracts.domain import RunResult
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import new_id
from oea.tools import ToolKit


def plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


class Agent:
    def __init__(self, *, name, model=None, instructions="", tools=None, skills=(), context=None,
                 workspace=None, execution=None, communication=None, manager=None, agent_id=None,
                 state_dir=None, host=None, client=None, project_id=None, session_id=None):
        require(not (host and client), "INVALID_REQUEST", "Choose a local host or an existing-host client")
        slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:63]
        if not slug or not slug[0].isalpha():
            slug = "agent-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        toolkit = tools if isinstance(tools, ToolKit) else ToolKit.local().with_tools(*(tools or ()))
        options = {"id": agent_id or slug, "name": name, "project_id": project_id, "model": model or Model.from_env(),
                   "instructions": Instructions(system=instructions) if isinstance(instructions, str) else instructions,
                   "tools": toolkit.bindings, "skills": skills, "workspace": workspace or Workspace(), "manager": manager}
        options.update({key: value for key, value in (("context", context), ("execution", execution), ("communication", communication)) if value is not None})
        self.definition = AgentDefinition.model_validate(options)
        self.toolkit = toolkit
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.backend = host or client
        self.owns_host = self.backend is None
        self._runner = self._loop = None
        self._ready = self._closed = self._closing = False
        self._initializing = None
        self._submissions = set()
        self._instances = {}
        self._session_id = session_id
        self._saved = False
        self._requested_project_id = project_id
        self._owned_commands = False

    @classmethod
    def from_saved(cls, agent_id, *, state_dir=None, host=None, client=None, project_id=None, session_id=None, allow_commands=False):
        """Lazily use an existing definition without creating or overwriting it."""
        require(host is not None or client is not None or state_dir is not None, "INVALID_REQUEST", "Choose an existing host/client or an explicit closed SDK state directory")
        require(not allow_commands or host is None and client is None, "INVALID_REQUEST", "An existing host controls its own command grant")
        value = cls(name=agent_id, agent_id=agent_id, model=Model(), tools=ToolKit.local(files=False), state_dir=state_dir,
                    host=host, client=client, project_id=project_id, session_id=session_id)
        value._saved, value._owned_commands = True, allow_commands
        return value

    def instance(self, name="default"):
        if name not in self._instances:
            self._instances[name] = AgentInstance(self, name)
        return self._instances[name]

    async def _call(self, operation, params):
        if hasattr(self.backend, "registry"):
            return plain(await self.backend.registry.call(operation, params))
        return await self.backend.call(operation, params)

    async def _ensure(self):
        require(not self._closed and not self._closing, "AGENT_CLOSED", "Agent lifecycle is closed")
        loop = asyncio.get_running_loop()
        require(self._loop in {None, loop}, "SDK_LOOP_MISMATCH", "Keep this Agent on its original event loop")
        self._loop = loop
        if self._ready:
            return
        if self._initializing is None:
            self._initializing = asyncio.create_task(self._initialize())
        await asyncio.shield(self._initializing)
        require(not self._closed and not self._closing, "AGENT_CLOSED", "Agent lifecycle is closed")

    async def _initialize(self):
        try:
            if self.owns_host:
                from oea.runtime.host import Host
                from oea.workspace.paths import default_data_dir
                directory = self.state_dir or default_data_dir() / "sdk" / self.definition.id
                backend = Host(directory, allow_commands=self._owned_commands if self._saved else "run_command" in self.definition.tools.local)
                if self._saved:
                    require(not (backend.root / "runtime.sqlite").exists(), "SCHEMA_MISMATCH", "Use offline migration before opening legacy state")
                    require((backend.root / "oea.sqlite").is_file(), "NOT_FOUND", "Saved SDK state is unavailable")
                self.backend = await backend.__aenter__()
            else:
                require(getattr(self.backend, "opened", False), "HOST_CLOSED", "Open the shared host or client before using this Agent")
            if self._saved:
                saved = await self._call("agents.get", {"agent_id": self.definition.id})
                self.definition = AgentDefinition.model_validate(saved["definition"])
                require(self._requested_project_id is None or self.definition.project_id in {None, self._requested_project_id}, "PROJECT_MISMATCH", "Saved Agent belongs to another project")
            if self.toolkit.providers:
                require(hasattr(self.backend, "register_tool"), "TOOL_REGISTRATION_REQUIRED", "Register Python functions in the existing host process before binding them remotely")
                for provider in self.toolkit.providers:
                    existing = self.backend.tools.providers.get(provider.describe().name)
                    if existing is None:
                        self.backend.register_tool(provider)
                    else:
                        require(existing is provider or getattr(existing, "function", None) is not None
                                and existing.function is getattr(provider, "function", None) and existing.describe() == provider.describe(),
                                "TOOL_CONFLICT", "This tool name is already bound to another implementation")
            session = await self._call("sessions.get", {"session_id": self._session_id}) if self._session_id else None
            if session:
                require(self.definition.project_id in {None, session["project_id"]} and self._requested_project_id in {None, session["project_id"]}, "PROJECT_MISMATCH", "Session belongs to another project")
            project_id = self.definition.project_id or (session["project_id"] if session else self._requested_project_id)
            require(project_id is not None or not self._saved, "PROJECT_REQUIRED", "Choose a project or session for this global Agent")
            project_id = project_id or "default"
            try:
                await self._call("projects.get", {"project_id": project_id})
            except RuntimeFault as error:
                if error.code != "NOT_FOUND" or self._saved:
                    raise
                await self._call("projects.create", {"project_id": project_id, "name": "Default" if project_id == "default" else project_id})
            definition = self.definition
            if not self._saved:
                definition = definition.model_copy(update={"project_id": project_id})
                saved = await self._call("agents.save", {"definition": definition.model_dump(mode="json")})
                definition = AgentDefinition.model_validate(saved["definition"])
            if session is None:
                identity = "sdk-" + self.definition.id if self.definition.project_id is not None or not self._saved else "sdk-" + hashlib.sha256((project_id + "\0" + self.definition.id).encode("utf-8")).hexdigest()[:24]
                try:
                    session = await self._call("sessions.get", {"session_id": identity})
                except RuntimeFault as error:
                    if error.code != "NOT_FOUND":
                        raise
                    try:
                        session = await self._call("sessions.create", {"project_id": project_id, "name": self.definition.name, "session_id": identity})
                    except RuntimeFault as conflict:
                        if conflict.code != "IDEMPOTENCY_CONFLICT":
                            raise
                        session = await self._call("sessions.get", {"session_id": identity})
                require(session["project_id"] == project_id, "PROJECT_MISMATCH", "SDK session belongs to another project")
            self._session_id = session["id"]
            self.definition = definition
            self._ready = True
        except BaseException as error:
            if self.owns_host and self.backend is not None:
                try:
                    await self.backend.aclose()
                except BaseException as cleanup_error:
                    raise error from cleanup_error
            raise

    def _sync(self, factory):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Use arun/astream/aclose inside an async application")
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(factory())

    def run(self, text, **options):
        return self.instance().run(text, **options)

    async def arun(self, text, **options):
        return await self.instance().arun(text, **options)

    def stream(self, text, **options):
        return self.instance().stream(text, **options)

    def astream(self, text, **options):
        return self.instance().astream(text, **options)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        if self._closed:
            return
        require(self._loop in {None, asyncio.get_running_loop()}, "SDK_LOOP_MISMATCH", "Close this Agent on its original event loop")
        self._closing = True
        try:
            if self._initializing is not None:
                await asyncio.gather(self._initializing, return_exceptions=True)
            if self._submissions:
                await asyncio.gather(*tuple(self._submissions), return_exceptions=True)
            if self.owns_host and self.backend is not None:
                await self.backend.aclose()
            self._closed = True
        finally:
            self._closing = False

    def close(self):
        if self._closed:
            return
        if self._runner is not None:
            self._sync(self.aclose)
            self._runner.close()
            self._runner = None
        elif self._loop is not None:
            raise RuntimeError("Use aclose for an Agent used asynchronously")
        else:
            self._closed = True


class AgentInstance:
    def __init__(self, agent: Agent, name):
        self.agent, self.name = agent, name
        self.id = self.last_run_id = None

    async def _ensure(self):
        await self.agent._ensure()
        if self.id is None:
            value = await self.agent._call("instances.create", {"session_id": self.agent._session_id, "agent_id": self.agent.definition.id, "name": self.name})
            self.id = value["id"]

    async def submit(self, text, *, idempotency_key=None, policy=None, budget_id=None, parent_run_id=None, artifact_ids=()):
        await self._ensure()
        spec = {"instance_id": self.id, "input": text, "idempotency_key": idempotency_key or new_id("work"), "budget_id": budget_id, "parent_run_id": parent_run_id, "artifact_ids": artifact_ids}
        if policy is not None:
            spec["policy"] = plain(policy)
        require(not self.agent._closed and not self.agent._closing, "AGENT_CLOSED", "Agent lifecycle is closed")
        async def accept():
            handle = await self.agent._call("runs.submit", {"spec": spec})
            self.last_run_id = handle["id"]
            return handle
        task = asyncio.create_task(accept())
        self.agent._submissions.add(task)
        def settled(completed):
            self.agent._submissions.discard(completed)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(settled)
        return await asyncio.shield(task)

    async def arun(self, text, *, idempotency_key=None, timeout=None, policy=None, budget_id=None, parent_run_id=None, artifact_ids=()):
        handle = await self.submit(text, idempotency_key=idempotency_key, policy=policy, budget_id=budget_id, parent_run_id=parent_run_id, artifact_ids=artifact_ids)
        value = await self.agent._call("runs.result", {"run_id": handle["id"], "timeout": timeout if timeout is not None else self.agent.definition.execution.budget.max_elapsed_seconds})
        return RunResult.model_validate(value)

    def run(self, text, **options):
        return self.agent._sync(lambda: self.arun(text, **options))

    async def astream(self, text, *, idempotency_key=None, policy=None, budget_id=None, parent_run_id=None, artifact_ids=()):
        handle = await self.submit(text, idempotency_key=idempotency_key, policy=policy, budget_id=budget_id, parent_run_id=parent_run_id, artifact_ids=artifact_ids)
        async with aclosing(self.agent.backend.events(handle["id"])) as events:
            async for frame in events:
                yield frame

    def stream(self, text, **options):
        iterator = self.astream(text, **options)
        try:
            while True:
                try:
                    yield self.agent._sync(lambda: anext(iterator))
                except StopAsyncIteration:
                    break
        finally:
            self.agent._sync(iterator.aclose)

    async def aresume(self, input=None, *, run_id=None, idempotency_key=None, timeout=None, refresh_model=True, refresh_capabilities=True):
        await self._ensure()
        identity = run_id or self.last_run_id
        require(identity is not None, "RUN_REQUIRED", "Choose the original run to resume")
        await self.agent._call("runs.resume", {"run_id": identity, "idempotency_key": idempotency_key or new_id("resume"), "input": input,
                                              "refresh_model": refresh_model, "refresh_capabilities": refresh_capabilities})
        self.last_run_id = identity
        value = await self.agent._call("runs.result", {"run_id": identity, "timeout": timeout if timeout is not None else self.agent.definition.execution.budget.max_elapsed_seconds})
        return RunResult.model_validate(value)

    def resume(self, input=None, **options):
        return self.agent._sync(lambda: self.aresume(input, **options))
