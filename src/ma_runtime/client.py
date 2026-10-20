"""Async remote SDK for an existing local platform host (no second Runtime)."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from .errors import RuntimeFault, require
from .models import (
    AgentConfig,
    InstanceBatchSpec,
    AgentSkill,
    MCPServerConfig,
    MessageSpec,
    TaskSpec,
    json_dump,
)
from .paths import default_data_dir, external_path


class _Namespace:
    def __init__(self, client, namespace):
        self.client, self.namespace = client, namespace

    async def call(self, method, **params):
        return await self.client.call(f"{self.namespace}.{method}", params)


class AgentClient(_Namespace):
    async def capabilities(self, agent_id):
        return await self.call("capabilities", agent_id=agent_id)
    async def list(self, project_id=None):
        return await self.call("list", project_id=project_id)

    async def get(self, agent_id):
        return await self.call("describe", agent_id=agent_id)

    async def configure(self, config: AgentConfig | dict, *, api_key=None):
        if isinstance(config, dict):
            config = {
                k: v
                for k, v in config.items()
                if k not in {"has_api_key", "descriptor", "active_runs", "model_setup"}
            }
        return await self.call(
            "configure", config=AgentConfig.model_validate(config), api_key=api_key
        )

    async def update(self, agent_id, **changes):
        config = await self.get(agent_id)
        return await self.configure({**config, **changes})

    async def test_model(self, agent_id):
        return await self.call("test_model", agent_id=agent_id)

    async def documents(self, agent_id):
        return await self.call("documents", agent_id=agent_id)

    async def set_instructions(self, agent_id, content):
        return await self.call("set_instructions", agent_id=agent_id, content=content)

    async def skills(self, agent_id):
        return await self.call("skills_list", agent_id=agent_id)

    async def put_skill(self, agent_id, skill: AgentSkill | dict):
        return await self.call(
            "skill_put", agent_id=agent_id, skill=AgentSkill.model_validate(skill)
        )

    async def import_skill(self, agent_id, skill_id, markdown, *, enabled=True):
        return await self.call(
            "skill_import",
            agent_id=agent_id,
            skill_id=skill_id,
            markdown=markdown,
            enabled=enabled,
        )

    async def delete_skill(self, agent_id, skill_id):
        return await self.call("skill_delete", agent_id=agent_id, skill_id=skill_id)


class MCPClient(_Namespace):
    async def register_directory(self, directory):
        return await self.call("register_directory", directory=str(directory))
    """Control the platform-owned MCP clients, shared with Agents and Codex."""

    async def list(self):
        return await super().call("list")

    async def get(self, server_id):
        return await super().call("get", server_id=server_id)

    async def configure(self, config: MCPServerConfig | dict, *, secret_values=None):
        if isinstance(config, dict):
            config = {
                k: v
                for k, v in config.items()
                if k
                not in {
                    "status",
                    "connections",
                    "active_calls",
                    "tool_count",
                    "server_info",
                    "capabilities",
                    "discovered_at",
                    "env_keys",
                    "header_keys",
                    "last_error",
                    "bound_agents",
                    "catalog",
                }
            }
        return await super().call(
            "configure",
            config=MCPServerConfig.model_validate(config),
            secret_values=secret_values,
        )

    async def connect(self, server_id, *, consumer_id="sdk"):
        return await super().call(
            "connect", server_id=server_id, consumer_id=consumer_id
        )

    async def disconnect(self, server_id):
        return await super().call("disconnect", server_id=server_id)

    async def delete(self, server_id):
        return await super().call("delete", server_id=server_id)

    async def tools(self, server_id, *, refresh=False, consumer_id="sdk"):
        return await super().call(
            "tools", server_id=server_id, refresh=refresh, consumer_id=consumer_id
        )

    async def call_tool(
        self,
        server_id,
        tool_name,
        arguments=None,
        *,
        consumer_id="sdk",
        idempotency_key=None,
    ):
        return await super().call(
            "call",
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments or {},
            consumer_id=consumer_id,
            idempotency_key=idempotency_key,
        )

    async def calls(self, server_id=None, *, limit=50):
        return await super().call("calls", server_id=server_id, limit=limit)

    async def resources(self, server_id, *, consumer_id="sdk", cursor=None):
        return await super().call(
            "resources", server_id=server_id, consumer_id=consumer_id, cursor=cursor
        )

    async def read_resource(self, server_id, uri, *, consumer_id="sdk"):
        return await super().call(
            "read_resource", server_id=server_id, uri=uri, consumer_id=consumer_id
        )

    async def prompts(self, server_id, *, consumer_id="sdk", cursor=None):
        return await super().call(
            "prompts", server_id=server_id, consumer_id=consumer_id, cursor=cursor
        )

    async def get_prompt(self, server_id, name, arguments=None, *, consumer_id="sdk"):
        return await super().call(
            "get_prompt",
            server_id=server_id,
            name=name,
            arguments=arguments,
            consumer_id=consumer_id,
        )

    async def codex_candidates(self):
        return await super().call("codex_candidates")

    async def import_codex(self, names):
        return await super().call("import_codex", names=list(names))

    async def codex_config(self):
        return await super().call("codex_config")


class SkillClient(_Namespace):
    async def list(self, *, cursor=None, limit=50):
        return await self.call("list", cursor=cursor, limit=limit)

    async def get(self, skill_id):
        return await self.call("get", skill_id=skill_id)

    async def register_directory(self, directory, *, enabled=True):
        return await self.call("register_directory", directory=str(directory), enabled=enabled)

    async def refresh(self, skill_id):
        return await self.call("refresh", skill_id=skill_id)

    async def set_enabled(self, skill_id, enabled):
        return await self.call("set_enabled", skill_id=skill_id, enabled=enabled)

    async def read(self, skill_id, path="SKILL.md"):
        return await self.call("read", skill_id=skill_id, path=path)

    async def delete(self, skill_id):
        return await self.call("delete", skill_id=skill_id)


class OperationsClient(_Namespace):
    async def bind(self, session_id, consumer_id, *, thread_id=None):
        return await self.call("bind", session_id=session_id, consumer_id=consumer_id, thread_id=thread_id)

    async def inspect(self, session_id, *, task_id=None):
        return await self.call("inspect", session_id=session_id, task_id=task_id)

    async def inbox(self, session_id, consumer_id, *, cursor=None, limit=20, timeout=0):
        return await self.call("inbox", session_id=session_id, consumer_id=consumer_id, cursor=cursor, limit=limit, timeout=timeout)

    async def read(self, session_id, event_id):
        return await self.call("read", session_id=session_id, event_id=event_id)

    async def report(self, session_id, summary, idempotency_key, *, details=""):
        return await self.call("report", session_id=session_id, summary=summary, idempotency_key=idempotency_key, details=details)

    async def reply(self, report_id, consumer_id, content, idempotency_key):
        return await self.call("reply", report_id=report_id, consumer_id=consumer_id, content=content, idempotency_key=idempotency_key)

    async def ack(self, session_id, consumer_id, cursor):
        return await self.call("ack", session_id=session_id, consumer_id=consumer_id, cursor=cursor)

    async def test(self, session_id, agent_id, input, idempotency_key, *, tool_policy=None, skill_policy=None):
        return await self.call("test", session_id=session_id, agent_id=agent_id, input=input, idempotency_key=idempotency_key,
                               tool_policy=tool_policy, skill_policy=skill_policy)


class ProjectClient(_Namespace):
    async def list(self, include_archived=False, include_deleted=False):
        return await self.call("list", include_archived=include_archived, include_deleted=include_deleted)

    async def get(self, project_id):
        return await self.call("get", project_id=project_id)

    async def create(self, name, local_folder, idempotency_key):
        return await self.call("create", name=name, local_folder=local_folder, idempotency_key=idempotency_key)

    async def update(self, project_id, **changes):
        return await self.call("update", project_id=project_id, **changes)

    async def archive(self, project_id, archived=True):
        return await self.call("archive", project_id=project_id, archived=archived)

    async def delete(self, project_id, *, confirmed=False):
        return await self.call("delete", project_id=project_id, confirmed=confirmed)

    async def restore(self, project_id):
        return await self.call("restore", project_id=project_id)

    async def adopt(self, project_id, session_ids, agent_ids):
        return await self.call("adopt", project_id=project_id, session_ids=session_ids, agent_ids=agent_ids)


class SessionClient(_Namespace):
    async def create(self, title, **options):
        return await self.call("create", title=title, **options)

    async def list(self, **options):
        return await self.call("list", **options)

    async def snapshot(self, session_id):
        return await self.call("snapshot", session_id=session_id)

    async def delete(self, session_id, *, confirmed=False):
        return await self.call("delete", session_id=session_id, confirmed=confirmed)

    async def restore(self, session_id):
        return await self.call("restore", session_id=session_id)


class ThreadClient(_Namespace):
    async def create(self, session_id, agent_id, title=None):
        return await self.call(
            "create", session_id=session_id, agent_id=agent_id, title=title
        )

    async def turn(self, thread_id, content, idempotency_key, **options):
        return await self.call(
            "turn",
            thread_id=thread_id,
            content=content,
            idempotency_key=idempotency_key,
            **options,
        )

    async def get(self, thread_id):
        return await self.call("get", thread_id=thread_id)

    async def fork(self, thread_id, title=None, **options):
        return await self.call("fork", thread_id=thread_id, title=title, **options)

    async def conversation(self, thread_id, **options):
        return await self.call("conversation", thread_id=thread_id, **options)


class TaskClient(_Namespace):
    async def submit(self, spec: TaskSpec | dict):
        return await self.call("submit", spec=TaskSpec.model_validate(spec))

    async def get(self, task_id, *, presentation=False):
        return await self.call("get", task_id=task_id, presentation=presentation)

    async def progress(self, task_id):
        return await self.call("progress", task_id=task_id)

    async def trace(self, task_id, **options):
        return await self.call("trace", task_id=task_id, **options)

    async def continue_result(self, task_id, idempotency_key, content=""):
        return await self.call("continue_result", task_id=task_id, idempotency_key=idempotency_key, content=content)

    async def result(self, task_id, *, timeout=0):
        return await self.call("result", task_id=task_id, timeout=timeout)

    async def cancel(self, task_id):
        return await self.call("cancel", task_id=task_id)

    async def workflow(self, session_id, nodes, idempotency_key):
        return await self.call(
            "workflow",
            session_id=session_id,
            nodes=nodes,
            idempotency_key=idempotency_key,
        )


class InstanceClient(_Namespace):
    async def spawn(self, spec: InstanceBatchSpec | dict):
        return await self.call("spawn", spec=InstanceBatchSpec.model_validate(spec))

    async def get(self, batch_id, *, include_outputs=False):
        return await self.call("get", batch_id=batch_id, include_outputs=include_outputs)

    async def list(self, session_id, **options):
        return await self.call("list", session_id=session_id, **options)

    async def result(self, batch_id, *, timeout=0):
        return await self.call("result", batch_id=batch_id, timeout=timeout)

    async def cancel(self, batch_id):
        return await self.call("cancel", batch_id=batch_id)


class MessageClient(_Namespace):
    async def send(self, spec: MessageSpec | dict):
        return await self.call("send", spec=MessageSpec.model_validate(spec))


class WorkflowClient(_Namespace):
    async def create(self, project_id, name, idempotency_key, *, graph=None, description=""):
        return await self.call("create", project_id=project_id, name=name, idempotency_key=idempotency_key,
                               graph=graph, description=description)

    async def get(self, workflow_id):
        return await self.call("get", workflow_id=workflow_id)

    async def list(self, project_id, **options):
        return await self.call("list", project_id=project_id, **options)

    async def update(self, workflow_id, expected_revision, **changes):
        return await self.call("update", workflow_id=workflow_id, expected_revision=expected_revision, **changes)

    async def archive(self, workflow_id, archived=True):
        return await self.call("archive", workflow_id=workflow_id, archived=archived)

    async def delete(self, workflow_id, *, confirmed=False):
        return await self.call("delete", workflow_id=workflow_id, confirmed=confirmed)

    async def restore(self, workflow_id):
        return await self.call("restore", workflow_id=workflow_id)

    async def validate(self, project_id, graph):
        return await self.call("validate", project_id=project_id, graph=graph)

    async def node_types(self):
        return await self.call("node_types")

    async def run(self, workflow_id, inputs, idempotency_key, *, expected_revision=None):
        return await self.call("run", workflow_id=workflow_id, inputs=inputs,
                               idempotency_key=idempotency_key, expected_revision=expected_revision)

    async def get_run(self, run_id):
        return await self.call("get_run", run_id=run_id)

    async def runs(self, workflow_id, **options):
        return await self.call("runs", workflow_id=workflow_id, **options)

    async def result(self, run_id, *, timeout=0):
        return await self.call("result", run_id=run_id, timeout=timeout)

    async def cancel(self, run_id):
        return await self.call("cancel", run_id=run_id)

    async def resume_node(self, run_id, node_id, input, idempotency_key, *, instance_index=None):
        return await self.call("resume_node", run_id=run_id, node_id=node_id,
                               input=input, idempotency_key=idempotency_key, instance_index=instance_index)

    async def reconcile_node(self, run_id, node_id, resolution, *, instance_index=None):
        return await self.call("reconcile_node", run_id=run_id, node_id=node_id, resolution=resolution, instance_index=instance_index)


class EnvironmentClient(_Namespace):
    async def list(self, project_id=None):
        return await self.call("list", project_id=project_id)

    async def get(self, environment_id):
        return await self.call("get", environment_id=environment_id)

    async def configure(self, config):
        return await self.call("configure", config=config)

    async def prepare(self, environment_id, idempotency_key):
        return await self.call("prepare", environment_id=environment_id, idempotency_key=idempotency_key)

    async def check(self, environment_id, idempotency_key):
        return await self.call("check", environment_id=environment_id, idempotency_key=idempotency_key)


class WorkbenchClient(_Namespace):
    async def inspect(self, *, agent_id=None, task_id=None):
        return await self.call("inspect", agent_id=agent_id, task_id=task_id)

    async def calls(self, task_id, *, cursor="", limit=25, tool_call_id=None):
        return await self.call("calls", task_id=task_id, cursor=cursor, limit=limit, tool_call_id=tool_call_id)

    async def reconcile(self, tool_call_id, resolution=None):
        return await self.call("reconcile", tool_call_id=tool_call_id, resolution=resolution)


class ContextClient(_Namespace):
    async def read(self, reference, project_id, *, offset=0, limit=8192):
        return await self.call("read", reference=reference, project_id=project_id, offset=offset, limit=limit)


class PlatformClient:
    def __init__(self, base_url, token, *, timeout=180, transport=None):
        target = urlsplit(base_url)
        require(
            target.scheme == "http"
            and target.hostname in {"127.0.0.1", "localhost", "::1"}
            and not target.username
            and not target.password
            and not target.query
            and not target.fragment,
            "PERMISSION_DENIED",
            "PlatformClient must connect to a local HTTP gateway",
        )
        self.http = httpx.AsyncClient(
            trust_env=False,
            base_url=base_url.rstrip("/"),
            headers={"Authorization": "Bearer " + token},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self.agents, self.mcp = AgentClient(self, "agents"), MCPClient(self, "mcp")
        self.skills = SkillClient(self, "skills")
        self.operations = OperationsClient(self, "operations")
        self.projects = ProjectClient(self, "projects")
        self.workflows = WorkflowClient(self, "workflows")
        self.instances = InstanceClient(self, "instances")
        self.environments = EnvironmentClient(self, "environments")
        self.workbench = WorkbenchClient(self, "workbench")
        self.contexts = ContextClient(self, "contexts")
        self.sessions, self.threads = (
            SessionClient(self, "sessions"),
            ThreadClient(self, "threads"),
        )
        self.tasks, self.messages = (
            TaskClient(self, "tasks"),
            MessageClient(self, "messages"),
        )

    @classmethod
    def from_data_dir(cls, data_dir=None, **options):
        root = external_path(data_dir or default_data_dir())
        metadata = json.loads((root / "client.json").read_text(encoding="utf-8"))
        token = (root / "client-token").read_text(encoding="utf-8").strip()
        return cls(metadata["url"], token, **options)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        await self.http.aclose()

    async def call(self, method, params=None):
        response = await self.http.post(
            "/api/rpc",
            content=json_dump({"method": method, "params": params or {}}),
            headers={"Content-Type": "application/json"},
        )
        value = response.json()
        if value.get("error"):
            error = value["error"]
            raise RuntimeFault(
                error.get("code", "REMOTE_ERROR"),
                error.get("safe_message", "Platform request failed"),
                retryable=error.get("retryable", False),
                details=error.get("details", {}),
            )
        response.raise_for_status()
        return value["result"]

    async def protocol(self):
        return await self.call("protocol.describe")

    async def events(self, session_id, *, after_seq=0):
        async with self.http.stream(
            "GET",
            f"/api/sessions/{session_id}/events",
            params={"after_seq": after_seq},
            timeout=None,
        ) as response:
            response.raise_for_status()
            data = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    yield json.loads("\n".join(data))
                    data.clear()
