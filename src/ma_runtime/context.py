"""Capabilities tied to a host-authenticated run, never a payload identity."""

import asyncio

from .errors import RuntimeFault, require
from .models import InstanceBatchSpec, MessageSpec, TaskSpec


class RunContext:
    def __init__(self, runtime, request, *, collaboration=True, model_client=None):
        from .llm import RunLLM

        self._runtime = runtime
        self.request = request
        self.collaboration = collaboration
        self.cancelled = asyncio.Event()
        self._mcp_tools = None
        self._workspace = None
        self.loaded_skills = {}
        self.used_tools = set()
        self.loaded_skill_ids = set()
        self.llm = RunLLM(self, model_client)

    async def initialize(self):
        from .store import rows
        required = (self.request.tool_policy.required or self.request.skill_policy.required or
                    self.request.tool_policy.mode == "required" or self.request.skill_policy.mode == "required")
        evidence = await self._runtime.store.call(lambda db: rows(db,
            "SELECT kind,name FROM execution_evidence WHERE task_id=?", (self.request.task_id,))) if required else []
        self.used_tools = {row.name for row in evidence if row.kind == "tool"}
        self.loaded_skill_ids = {row.name for row in evidence if row.kind == "skill"}
        for skill_id in self.request.skill_policy.required:
            self.loaded_skills[skill_id] = await self.read_skill(skill_id)

    def check_tool(self, name):
        require(self.request.tool_policy.permits(name), "PERMISSION_DENIED", "Tool is disabled by this task's policy")

    async def record_use(self, kind, name):
        from .execution_policy import record
        policy = self.request.tool_policy if kind == "tool" else self.request.skill_policy
        if kind == "tool":
            self.used_tools.add(name)
        if not policy.required and policy.mode != "required":
            return
        def save(db):
            self._runtime.check_run(db, self.request.run_id)
            record(db, self.request.task_id, self.request.run_id, kind, name)
        await self._runtime.store.call(save, transaction=True)

    async def list_tools(self):
        await self.check_current()
        if self._mcp_tools is None:
            self._mcp_tools = await self._runtime.mcp.schemas_for_run(
                self.request.run_id
            )
        return {**self._mcp_tools, "tools": [*self._mcp_tools["tools"], *self._runtime.workbench.catalog(self.request)]}

    async def list_skills(self):
        from .profiles import skill_catalog

        await self.check_current()
        return skill_catalog(self.request.skills)

    async def read_skill(self, skill_id, path="SKILL.md"):
        from .profiles import read_skill

        await self.check_current()
        current = self._runtime.agent_configs.get(self.request.agent_id)
        require(any(s.id == skill_id and s.enabled for s in self._runtime.skills.for_agent(current)),
                "PERMISSION_DENIED", "Skill is no longer enabled for this Agent")
        require(self.request.skill_policy.permits(skill_id), "PERMISSION_DENIED", "Skill is disabled by this task's policy")
        value = read_skill(self.request.skills, skill_id, path)
        if path == "SKILL.md":
            await self.record_use("skill", skill_id)
            self.loaded_skills[skill_id] = value
            self.loaded_skill_ids.add(skill_id)
        return value

    async def check_current(self):
        await self._runtime.store.call(
            lambda db: self._runtime.check_run(db, self.request.run_id)
        )

    async def report_issue(self, summary, idempotency_key, details="", wait=False):
        return await self._runtime.operations.report(self.request.session_id, summary, idempotency_key,
            details=details, wait=wait, caller_run_id=self.request.run_id)

    async def operations(self, action, params=None):
        import inspect
        from .operations import OPERATIONS_ACTIONS
        from .services import need

        await self.check_current()
        consumer = self.request.operations_consumer_id
        require(consumer and self._runtime.operations.for_thread(self.request.session_id, self.request.thread_id) == consumer,
                "PERMISSION_DENIED", "This execution is not the current operations coordinator")
        require(params is None or isinstance(params, dict), "INVALID_REQUEST", "Operations parameters must be an object")
        params = dict(params or {})
        actions = set(OPERATIONS_ACTIONS) - {"bind", "report"}
        if action == "guide":
            selected = params.get("action")
            require(selected in actions, "INVALID_REQUEST", "Choose one coordinator action")
            fn = getattr(self._runtime.operations, selected)
            return {"action": selected, "parameters": [{"name": p.name, "required": p.default is inspect.Parameter.empty,
                **({"default": p.default} if p.default is not inspect.Parameter.empty else {}),
                **({"maximum": 0} if p.name == "timeout" else {})}
                for p in inspect.signature(fn).parameters.values() if p.name not in {"session_id", "consumer_id", "caller_run_id"}]}
        require(action in actions, "PERMISSION_DENIED", "Operations action is unavailable to this coordinator")
        require(not ({"session_id", "consumer_id", "caller_run_id"} & set(params)), "PERMISSION_DENIED", "Operations scope is supplied by the host")
        if action == "reply":
            report = await self._runtime.store.call(lambda db: need(db, "operations_reports", params.get("report_id")))
            require(report.session_id == self.request.session_id, "PERMISSION_DENIED", "Report belongs to another session")
        else:
            params["session_id"] = self.request.session_id
        if action in {"inbox", "ack", "reply"}:
            params["consumer_id"] = consumer
        if action == "inbox":
            require(not params.get("timeout"), "UNSUPPORTED_CAPABILITY", "An active coordinator suspends for messages instead of blocking a run")
        if action == "test":
            self.check_tool("spawn_instances")
            require(self.collaboration, "PERMISSION_DENIED", "This coordinator cannot dispatch diagnostic work")
            params["caller_run_id"] = self.request.run_id
        fn = getattr(self._runtime.operations, action)
        try:
            inspect.signature(fn).bind(**params)
        except TypeError:
            raise RuntimeFault("INVALID_REQUEST", "Inspect the selected operations action parameters") from None
        return await fn(**params)

    async def list_threads(self):
        self.check_tool("list_threads")
        require(self.collaboration, "PERMISSION_DENIED", "This Agent cannot inspect collaboration threads")
        await self.check_current()
        result = (await self._runtime.sessions.snapshot(self.request.session_id))[
            "threads"
        ]
        await self.record_use("tool", "list_threads")
        return result

    async def send_message(self, spec: MessageSpec | dict):
        self.check_tool("send_message")
        require(
            self.collaboration,
            "PERMISSION_DENIED",
            "This agent cannot send collaboration messages",
        )
        spec = MessageSpec.model_validate(spec)
        require(
            spec.session_id == self.request.session_id,
            "PERMISSION_DENIED",
            "Cross-session message is not allowed",
        )
        spec = spec.model_copy(
            update={
                "source_thread_id": self.request.thread_id,
                "source_task_id": self.request.task_id,
                "source_run_id": self.request.run_id,
            }
        )
        result = await self._runtime.messages.send(
            spec, caller_run_id=self.request.run_id
        )
        await self.record_use("tool", "send_message")
        return result

    async def submit_task(self, spec: TaskSpec | dict):
        self.check_tool("submit_task")
        require(
            self.collaboration,
            "PERMISSION_DENIED",
            "This agent cannot submit child tasks",
        )
        spec = TaskSpec.model_validate(spec)
        require(
            spec.session_id == self.request.session_id,
            "PERMISSION_DENIED",
            "Cross-session task is not allowed",
        )
        result = await self._runtime.tasks.submit(spec, caller_run_id=self.request.run_id)
        await self.record_use("tool", "submit_task")
        return result

    async def wait_result(self, task_id):
        raise RuntimeFault(
            "UNSUPPORTED_CAPABILITY",
            "An active agent must suspend to wait for another task",
        )

    async def spawn_instances(self, spec: InstanceBatchSpec | dict):
        self.check_tool("spawn_instances")
        spec = InstanceBatchSpec.model_validate(spec)
        require(self.collaboration and spec.session_id == self.request.session_id,
                "PERMISSION_DENIED", "Child instances must stay in the current collaboration session")
        result = await self._runtime.instances.spawn(spec, caller_run_id=self.request.run_id)
        await self.record_use("tool", "spawn_instances")
        return result

    async def put_file(self, path, *, metadata=None):
        self.check_tool("put_file")
        await self.check_current()
        from .workspace import Workspace
        require(self.request.workbench, "PERMISSION_DENIED", "File delivery requires a workbench source grant")
        self._runtime.workbench.authorize(self.request, "publish_artifact")
        path = Workspace(self._runtime, self.request.workbench, self.request.session_id).path(str(path))
        result = await self._runtime.artifacts.put_file(
            self.request.session_id, path, metadata=metadata
        )
        await self.record_use("tool", "put_file")
        return result

    async def put_bytes(self, name, data, *, metadata=None):
        self.check_tool("put_bytes")
        await self.check_current()
        result = await self._runtime.artifacts.put_bytes(
            self.request.session_id, name, data, metadata=metadata
        )
        await self.record_use("tool", "put_bytes")
        return result

    async def set_run_ref(self, native_ref):
        import json
        import os

        from .models import json_dump

        native_ref = self._runtime.clean_json(native_ref)

        def save(db):
            _, run = self._runtime.check_run(db, self.request.run_id)
            previous = (
                json.loads(run.adapter_run_ref_json) if run.adapter_run_ref_json else {}
            )
            value = {
                **previous,
                **native_ref,
                "run_id": self.request.run_id,
                "host_pid": os.getpid(),
                "scopes": previous.get("scopes", {}),
            }
            db.execute(
                "UPDATE runs SET adapter_run_ref_json=? WHERE id=?",
                (json_dump(value), self.request.run_id),
            )

        await self._runtime.store.call(save, transaction=True)

    async def invoke_tool(self, name, arguments, idempotency_key):
        from .execution_policy import mcp_name
        provider = self._runtime.tools.providers.get(name)
        identifier = mcp_name(provider.server_id, provider.tool["name"]) if hasattr(provider, "server_id") else name
        self.check_tool(identifier)
        if name.startswith("mcp_"):
            catalog = await self.list_tools()
            require(
                any(t["function"]["name"] == name for t in catalog["tools"]),
                "PERMISSION_DENIED",
                "MCP tool is not granted to this execution",
            )
        result = await self._runtime.tools.invoke(
            name, arguments, idempotency_key, caller_run_id=self.request.run_id
        )
        if not result.get("isError"):
            self.used_tools.add(identifier)
        return result

    async def commit_boundary(self, event, local_seq):
        return await self._runtime.commit_boundary(
            self.request.run_id, event, local_seq
        )
