"""Capabilities tied to a host-authenticated run, never a payload identity."""

import asyncio

from .errors import RuntimeFault, require
from .models import InstanceBatchSpec, MessageSpec, TaskSpec, TaskResult, new_id, content_hash, json_dump, now_ms


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
        self.channel = None

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
        from .execution_policy import prepare_required_tools
        await prepare_required_tools(self)

    def check_tool(self, name):
        require(self.request.tool_policy.permits(name), "PERMISSION_DENIED", "Tool is disabled by this task's policy")
        require(not self.request.continuation or name not in {"send_message", "submit_task", "spawn_instances"},
                "CONTINUATION_ACTION_RESTRICTED", "Delivery continuation does not start new collaboration actions")

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
        from .context_projection import READ_CONTEXT_TOOL
        from .chat_history import READ_HISTORY_TOOL
        history = [READ_HISTORY_TOOL] if (self.request.resume_ref or {}).get("history_truncated") else []
        return {**self._mcp_tools, "tools": [*self._mcp_tools["tools"], *self._runtime.workbench.catalog(self.request), READ_CONTEXT_TOOL, *history]}

    async def retain_partial_result(self, result):
        value = result.model_dump(mode="json") if isinstance(result, TaskResult) else result
        result = TaskResult.model_validate(self._runtime.vault.redact(self._runtime.clean_json(value, limit=524288)))
        def retain(db):
            self._runtime.check_run(db, self.request.run_id)
            self._runtime.check_artifacts(db, self.request.session_id, result.artifact_ids)
            db.execute("INSERT INTO run_partial_results VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET result_json=excluded.result_json,updated_at=excluded.updated_at",
                       (self.request.run_id, json_dump(result), now_ms()))
        await self._runtime.store.call(retain, transaction=True)
        self.partial_model_result = result
        return {"retained": True, "run_id": self.request.run_id}

    async def read_history(self, cursor=None, limit=20):
        await self._charge_tool("read_history")
        checkpoint = (self.request.resume_ref or {}).get("checkpoint_id")
        require(checkpoint, "UNSUPPORTED_CAPABILITY", "No native message history was assigned to this run")
        result = await self._runtime.threads.history(self.request.thread_id, checkpoint_id=checkpoint, cursor=cursor, limit=limit)
        await self._record_progress("read_history", result)
        return result

    async def read_context(self, reference, offset=0, limit=8192, *, _parent_stage=None):
        from .tracing import RunStage
        stage = RunStage(self, "context_read", reference=reference, offset=offset, parent_stage_id=_parent_stage)
        await self._charge_tool("read_context", identity=stage.id)
        try:
            result = await self._runtime.contexts.read_for_run(self, reference, offset, limit)
        except RuntimeFault as exc:
            await stage.record("FAILED", exc)
            raise
        stage.attributes["bytes_read"] = len(result["text"].encode("utf-8"))
        await stage.record("SUCCEEDED")
        await self._record_progress("read_context", {key: result[key] for key in ("sha256", "offset", "text")})
        return result

    async def _charge_tool(self, name, *, key=None, identity=None):
        identity = identity or ("operation:" + content_hash([self.request.task_id, name, key]) if key else new_id("operation"))
        def charge(db):
            self._runtime.check_run(db, self.request.run_id)
            self._runtime.budgets.tool(db, self.request, identity, name)
        try:
            await self._runtime.store.call(charge, transaction=True)
        except RuntimeFault as exc:
            self._runtime.budgets.reject(self, exc)
            raise

    async def _record_progress(self, source, value):
        def record(db):
            self._runtime.check_run(db, self.request.run_id)
            self._runtime.budgets.progress(db, self.request.task_id, source, value)
        await self._runtime.store.call(record, transaction=True)

    async def tool_parent(self, name, idempotency_key, model_call_id=None, tool_call_id=None):
        causes = getattr(self, "model_tool_causes", {})
        if model_call_id:
            require(isinstance(model_call_id, str) and isinstance(tool_call_id, str), "INVALID_REQUEST", "Model tool provenance requires both call IDs")
            if causes.get((model_call_id, tool_call_id)) == name:
                return model_call_id
            from .store import one
            def verify(db):
                self._runtime.check_run(db, self.request.run_id)
                record = one(db, "SELECT payload_json FROM events WHERE task_id=? AND type='run.stage' "
                    "AND json_extract(payload_json,'$.id')=? AND json_extract(payload_json,'$.state')='SUCCEEDED' ORDER BY seq DESC LIMIT 1",
                    (self.request.task_id, model_call_id))
                import json
                return record and any(call["id"] == tool_call_id and call["name"] == name
                    for call in json.loads(record.payload_json).get("attributes", {}).get("tool_calls", []))
            require(await self._runtime.store.call(verify), "ADAPTER_PROTOCOL_ERROR", "Tool provenance does not match this task's recorded model request")
            return model_call_id
        matches = {stage_id for (stage_id, call_id), function in causes.items() if function == name
                   and idempotency_key in {self.request.task_id + ":" + call_id, self.request.task_id + ":graph:" + call_id}}
        return next(iter(matches)) if len(matches) == 1 else None

    async def project_context(self, value):
        await self.check_current()
        return await self._runtime.contexts.project_data(self, value)

    async def list_skills(self):
        from .profiles import skill_catalog

        await self.check_current()
        return skill_catalog(self.request.skills)

    async def read_skill(self, skill_id, path="SKILL.md"):
        from .profiles import read_skill

        await self._charge_tool("read_skill")
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
        await self._charge_tool("list_threads")
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
        await self._charge_tool("send_message", key=spec.idempotency_key)
        result = await self._runtime.messages.send(
            spec, caller_run_id=self.request.run_id
        )
        await self.record_use("tool", "send_message")
        return result

    async def reject_message(self, message_id, reason, idempotency_key):
        return await self._runtime.messages.reject(message_id, reason, idempotency_key, caller_run_id=self.request.run_id)

    async def _channel(self):
        if self.channel is None:
            self.channel = await self._runtime.channels.bind(self.request.run_id, self.request.thread_id,
                "in-process:" + self.request.run_id, capabilities=("generation", "inbox", "receipts", "reconnect"))
        return self.channel

    async def reconnect_channel(self, connection_id):
        previous = await self._channel()
        self.channel = await self._runtime.channels.bind(self.request.run_id, self.request.thread_id,
            connection_id, capabilities=previous["capabilities"], expected_generation=previous["generation"])
        return self.channel

    async def inbox(self, after_position=0, limit=32):
        return await self._runtime.channels.inbox(await self._channel(), after_position=after_position, limit=limit)

    async def acknowledge_messages(self, positions):
        return await self._runtime.channels.acknowledge(await self._channel(), positions)

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
        await self._charge_tool("submit_task", key=spec.idempotency_key)
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
        await self._charge_tool("spawn_instances", key=spec.idempotency_key)
        result = await self._runtime.instances.spawn(spec, caller_run_id=self.request.run_id)
        await self.record_use("tool", "spawn_instances")
        return result

    async def put_file(self, path, *, metadata=None):
        self.check_tool("put_file")
        await self._charge_tool("put_file")
        from .workspace import Workspace
        require(self.request.workbench, "PERMISSION_DENIED", "File delivery requires a workbench source grant")
        self._runtime.workbench.authorize(self.request, "publish_artifact")
        path = Workspace(self._runtime, self.request.workbench, self.request.session_id).path(str(path))
        result = await self._runtime.artifacts.put_file(
            self.request.session_id, path, metadata=metadata, caller_run_id=self.request.run_id
        )
        await self.record_use("tool", "put_file")
        return result

    async def put_bytes(self, name, data, *, metadata=None):
        self.check_tool("put_bytes")
        await self._charge_tool("put_bytes")
        result = await self._runtime.artifacts.put_bytes(
            self.request.session_id, name, data, metadata=metadata, caller_run_id=self.request.run_id
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
                "conversation_cutoff": self.request.conversation_cutoff,
                "scopes": previous.get("scopes", {}),
            }
            db.execute(
                "UPDATE runs SET adapter_run_ref_json=? WHERE id=?",
                (json_dump(value), self.request.run_id),
            )

        await self._runtime.store.call(save, transaction=True)

    async def invoke_tool(self, name, arguments, idempotency_key, *, model_call_id=None, tool_call_id=None):
        parent = await self.tool_parent(name, idempotency_key, model_call_id, tool_call_id)
        if name == "read_context":
            return await self.read_context(**arguments, _parent_stage=parent)
        if name == "read_history":
            return await self.read_history(**arguments)
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
            name, arguments, idempotency_key, caller_run_id=self.request.run_id, parent_stage_id=parent
        )
        if not result.get("isError"):
            self.used_tools.add(identifier)
        return result

    async def commit_boundary(self, event, local_seq):
        return await self._runtime.commit_boundary(
            self.request.run_id, event, local_seq
        )
