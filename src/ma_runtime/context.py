"""Capabilities tied to a host-authenticated run, never a payload identity."""

import asyncio

from .errors import RuntimeFault, require
from .models import MessageSpec, TaskSpec


class RunContext:
    def __init__(self, runtime, request, *, collaboration=True, model_client=None):
        from .llm import RunLLM

        self._runtime = runtime
        self.request = request
        self.collaboration = collaboration
        self.cancelled = asyncio.Event()
        self._mcp_tools = None
        self.llm = RunLLM(self, model_client)

    async def list_tools(self):
        await self.check_current()
        if self._mcp_tools is None:
            self._mcp_tools = await self._runtime.mcp.schemas_for_run(
                self.request.run_id
            )
        return self._mcp_tools

    async def list_skills(self):
        from .profiles import skill_catalog

        await self.check_current()
        return skill_catalog(self.request.skills)

    async def read_skill(self, skill_id, path="SKILL.md"):
        from .profiles import read_skill

        await self.check_current()
        return read_skill(self.request.skills, skill_id, path)

    async def check_current(self):
        await self._runtime.store.call(
            lambda db: self._runtime.check_run(db, self.request.run_id)
        )

    async def list_threads(self):
        await self.check_current()
        return (await self._runtime.sessions.snapshot(self.request.session_id))[
            "threads"
        ]

    async def send_message(self, spec: MessageSpec | dict):
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
        return await self._runtime.messages.send(
            spec, caller_run_id=self.request.run_id
        )

    async def submit_task(self, spec: TaskSpec | dict):
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
        return await self._runtime.tasks.submit(spec, caller_run_id=self.request.run_id)

    async def wait_result(self, task_id):
        raise RuntimeFault(
            "UNSUPPORTED_CAPABILITY",
            "An active agent must suspend to wait for another task",
        )

    async def put_file(self, path, *, metadata=None):
        await self.check_current()
        return await self._runtime.artifacts.put_file(
            self.request.session_id, path, metadata=metadata
        )

    async def put_bytes(self, name, data, *, metadata=None):
        await self.check_current()
        return await self._runtime.artifacts.put_bytes(
            self.request.session_id, name, data, metadata=metadata
        )

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
        if name.startswith("mcp_"):
            catalog = await self.list_tools()
            require(
                any(t["function"]["name"] == name for t in catalog["tools"]),
                "PERMISSION_DENIED",
                "MCP tool is not granted to this execution",
            )
        return await self._runtime.tools.invoke(
            name, arguments, idempotency_key, caller_run_id=self.request.run_id
        )

    async def commit_boundary(self, event, local_seq):
        return await self._runtime.commit_boundary(
            self.request.run_id, event, local_seq
        )
