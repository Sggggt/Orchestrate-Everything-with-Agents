"""A small MCP surface with on-demand contracts and bounded, session-local output."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections import OrderedDict

from .. import models, services
from ..brand import PRODUCT_NAME, SKILL_NAME
from ..artifacts import ArtifactStore
from ..errors import RuntimeFault, require
from ..events import EventHub
from ..mcp_service import MCPRegistry
from ..resources import ResourceCoordinator
from ..workflows import Workflows
from ..instances import Instances
from ..skill_registry import SkillRegistry
from ..operations import Operations, OPERATIONS_ACTIONS
from ..execution import Environments
from ..workspace import Workbench
from ..context_projection import ContextStore
from ..channels import ExecutionChannels
from ..a2a import A2AMapping
from ..budgets import Budgets
from ..admission import Admission

AREAS = {
    "admission": ("有界能力摘要、同项目候选硬约束和只读任务预检；不创建对象或占用容量", {name: "admission." + name for name in ("describe", "candidates", "check")}),
    "budgets": ("会话内累计模型、token、工具和时间预算；子任务与修复继承同一账本", {name: "budgets." + name for name in ("create", "get", "for_task", "entries", "amend")}),
    "a2a": ("A2A v1.0.1 内部对象映射；消息与执行意图分开；无网络绑定", {name: "a2a." + name for name in ("capabilities", "send", "get_task")}),
    "channels": ("执行逻辑通道、当前代次与连续传输确认；连接不创建执行者", {"get": "channels.get"}),
    "resources": ("数量容量、组合准入预检和持久租约；未知执行保留隔离占用", {name: "resources." + name for name in ("inspect", "configure", "preflight", "request", "get", "release_lease")}),
    "contexts": ("按引用读取项目内上下文证据；固定来源与版本显式合并", {name: "contexts." + name for name in ("read", "merge", "get_merge")}),
    "environments": ("外部受限执行环境与受管准备任务", {name: "environments." + name for name in ("list", "get", "configure", "prepare", "check")}),
    "workbench": ("Agent 工作能力、目录映射与原生工具回执", {name: "workbench." + name for name in ("inspect", "calls", "reconcile")}),
    "projects": (
        "项目、本地文件夹、项目内会话和 Agent",
        {name: "projects." + name for name in ("create", "get", "list", "update", "archive", "adopt", "delete", "restore")},
    ),
    "control": (
        "运行状态、执行预算、事件与资源",
        {
            "status": "@status",
            "config": "@config",
            "configure": "@configure",
            "events": "events.list",
            "live": "events.live",
            "resources": "resources.inspect",
            "read_result": "@read_result",
        },
    ),
    "agents": (
        "注册、查看和更新 Agent 执行端",
        {
            "list": "agents.list",
            "describe": "agents.describe",
            "capabilities": "agents.capabilities",
            "configure": "agents.configure",
            "test_model": "agents.test_model",
            "patch": "@agent_patch",
            "delete_secret": "@delete_secret",
        },
    ),
    "skills": (
        "共享标准 Skill 目录、正文、参考文件、启停与刷新；Agent 用 skill_bindings 关联",
        {name: "skills." + name for name in ("list", "get", "register_directory", "refresh", "set_enabled", "read", "delete")},
    ),
    "profiles": (
        "AGENT.md 与各 Agent 独立的 Skills",
        {
            "documents": "agents.documents",
            "set_instructions": "agents.set_instructions",
            "skills_list": "agents.skills_list",
            "skill_put": "agents.skill_put",
            "skill_import": "agents.skill_import",
            "skill_delete": "agents.skill_delete",
            "skill_parse": "@skill_parse",
        },
    ),
    "operations": (
        "双向运维、精简收件箱、指导与受管诊断测试；正文按引用读取",
        {name: "operations." + name for name in OPERATIONS_ACTIONS},
    ),
    "sessions": (
        "创建、查看和关闭项目内会话",
        {
            name: "sessions." + name
            for name in ("create", "get", "list", "snapshot", "close", "delete", "restore")
        },
    ),
    "threads": (
        "Agent 独立对话、继续执行、上下文与分支",
        {
            name: "threads." + name
            for name in ("create", "get", "ownership", "instances", "activity", "mark_read", "conversation", "history", "turn", "fork", "prepare_workdir", "workdir")
        },
    ),
    "tasks": (
        "任务、依赖工作流、取消、恢复和结果核对",
        {
            name: "tasks." + name
            for name in (
                "submit",
                "workflow",
                "get",
                "progress",
                "trace",
                "continue_result",
                "list",
                "result",
                "cancel",
                "retry",
                "resume",
                "reconcile",
            )
        },
    ),
    "messages": (
        "同一会话内传递完整消息、检查回执",
        {name: "messages." + name for name in ("send", "get", "list", "delivery", "reject", "expire")},
    ),
    "instances": (
        "受管子 Agent 多实例、Agent 池路由、批次预算、结果与取消",
        {name: "instances." + name for name in ("spawn", "get", "list", "result", "cancel")},
    ),
    "workflows": (
        "项目工作流、画布节点/连线、变量、校验与版本运行记录",
        {name: "workflows." + name for name in (
            "create", "get", "list", "update", "archive", "delete", "restore", "validate", "node_types",
            "run", "get_run", "runs", "activity", "result", "cancel", "resume_node", "reconcile_node", "repair", "get_repair",
        )},
    ),
    "artifacts": (
        "工作产物元数据及本机文件传输",
        {
            "get": "artifacts.get",
            "upload_file": "@upload_file",
            "download_file": "@download_file",
        },
    ),
    "mcp": (
        "共享 MCP 注册、连接、工具、资源、提示与调用记录",
        {
            name: "mcp." + name
            for name in (
                "list",
                "get",
                "configure",
                "register_directory",
                "configure_policy",
                "policy",
                "reconcile_capacity",
                "delete",
                "connect",
                "disconnect",
                "tools",
                "catalog",
                "tool",
                "call",
                "calls",
                "resources",
                "read_resource",
                "prompts",
                "get_prompt",
                "codex_candidates",
                "import_codex",
                "codex_config",
            )
        },
    ),
    "development": ("开发执行端与 SDK 协议；源码由本机文件工具编写", {}),
    "maintenance": ("启动、离线备份、升级和排错；不提供任意 Shell 或进程终止", {}),
}

OWNERS = {
    "admission": Admission,
    "budgets": Budgets,
    "a2a": A2AMapping,
    "channels": ExecutionChannels,
    "contexts": ContextStore,
    "environments": Environments,
    "workbench": Workbench,
    "projects": services.Projects,
    "sessions": services.Sessions,
    "agents": services.Agents,
    "threads": services.Threads,
    "tasks": services.Tasks,
    "messages": services.Messages,
    "artifacts": ArtifactStore,
    "events": EventHub,
    "resources": ResourceCoordinator,
    "mcp": MCPRegistry,
    "skills": SkillRegistry,
    "operations": Operations,
    "workflows": Workflows,
    "instances": Instances,
}
PARAM_MODELS = {
    ("admission.check", "spec"): models.TaskSpec,
    ("budgets.create", "policy"): models.BudgetPolicy,
    ("budgets.amend", "policy"): models.BudgetPolicy,
    ("mcp.configure_policy", "policy"): models.MCPResourcePolicy,
    ("resources.configure", "config"): models.ResourceCapacity,
    ("resources.request", "spec"): models.ResourceLeaseSpec,
    ("contexts.merge", "spec"): models.ContextMergeSpec,
    ("workbench.reconcile", "resolution"): models.Reconciliation,
    ("environments.configure", "config"): models.EnvironmentConfig,
    ("operations.test", "tool_policy"): models.UsePolicy,
    ("operations.test", "skill_policy"): models.UsePolicy,
    ("agents.configure", "config"): models.AgentConfig,
    ("agents.skill_put", "skill"): models.AgentSkill,
    ("tasks.submit", "spec"): models.TaskSpec,
    ("instances.spawn", "spec"): models.InstanceBatchSpec,
    ("tasks.reconcile", "resolution"): models.Reconciliation,
    ("messages.send", "spec"): models.MessageSpec,
    ("mcp.configure", "config"): models.MCPServerConfig,
    ("workflows.create", "graph"): models.WorkflowGraph,
    ("workflows.update", "graph"): models.WorkflowGraph,
    ("workflows.validate", "graph"): models.WorkflowGraph,
    ("workflows.reconcile_node", "resolution"): models.Reconciliation,
}
DEV_MODELS = {
    "workbench": models.WorkbenchConfig,
    "environment": models.EnvironmentConfig,
    "graph_adapter": models.GraphAdapterConfig,
    "result": models.TaskResult,
    "project": models.ProjectInfo,
    "request": models.ExecutionRequest,
    "descriptor": models.AgentDescriptor,
    "event": models.AdapterEvent,
    "task": models.TaskSpec,
    "message": models.MessageSpec,
    "config": models.AgentConfig,
    "llm_request": models.LLMRequest,
    "llm_response": models.LLMResponse,
    "workflow_graph": models.WorkflowGraph,
    "workflow_node": models.WorkflowNode,
    "workflow_run": models.WorkflowRunSnapshot,
    "instance_batch": models.InstanceBatchSpec,
}
# Virtual operations use existing authenticated HTTP routes; no server restart is needed.
EXTRA_PARAMS = {
    "@status": {},
    "@config": {},
    "@configure": {"changes": ...},
    "@agent_patch": {"agent_id": ..., "changes": ...},
    "@delete_secret": {"agent_id": ...},
    "@skill_parse": {"skill_id": ..., "markdown": ...},
    "@upload_file": {"session_id": ..., "path": ...},
    "@download_file": {"artifact_id": ..., "path": ..., "overwrite": False},
    "@read_result": {"result_ref": ..., "pointer": "", "offset": 0, "limit": 4000},
}
NOTES = {
    "budgets.amend": "Explicit administrative limit change. Read the current policy_hash first; submit the complete policy, stable idempotency_key and reason. Usage, start time, progress counters and inherited bindings are preserved. Do not use amendments as an automatic response to budget exhaustion.",
    "admission.describe": "Bounded configured purpose, actual adapter declarations, grants, capacity and latest 32 model observations for current settings. Configuration is not verified expertise. Prices and matched workloads are absent, so no cost/speed ranking or semantic quality claim is made.",
    "admission.candidates": "Required project_id. Pages include eligible candidates and explicit exclusions; capabilities accepts supports_suspend/supports_fork/supports_probe. Hard constraints first; capacity exhaustion means wait, not a new host. Unknown live availability is reported. No automatic selection or execution.",
    "admission.check": "Read-only TaskSpec preflight against shared submission rules, cached dependencies, current capacity and optional budget_id. No objects, connections, reservations or model calls. Unknown model/upstream behavior is not success. Submission and execution recheck current state. An identical existing key returns its receipt; do not resubmit unknown effects with a new key.",
    "mcp.catalog": "Read a cached paginated tool directory with stable identifiers, protocol version, schema fingerprints and estimated input size. Never connects. If discovery is missing, explicitly connect first. Use mcp.tool for one complete schema including recursive definitions.",
    "mcp.tool": "Read one complete cached tool schema, optionally requiring an expected_fingerprint. Includes local schema validation, protocol version and size. Provider compatibility and upstream execution are separate and remain unverified. Full schema preserves all local references; stale fingerprints require reinspection.",
    "threads.fork": "Fork a committed checkpoint or result message with a stable idempotency_key. Omit selectors for latest; the receipt freezes that resolved checkpoint. Replays return the same branch. Shared prefixes exclude later inputs and evidence. Native graph adapters must explicitly implement safe completed-state forks.",
    "tasks.continue_result": "Explicitly repair a failed standalone delivery in its unchanged thread, preserving task identity and tool keys. Requires a committed checkpoint and no unresolved effects. Reuse the same key after disconnects. Frozen run/call/token/time limits include summary requests. New external actions are blocked; inspect the retained partial result and trace before choosing recovery.",
    "contexts.read": "Read one source reference in an explicit project, using bounded UTF-8 byte offsets. Follow next_offset for continuation. References identify immutable evidence snapshots, not current file contents or permission grants. Do not load every source or replace full results with UI summaries.",
    "tasks.trace": "Read one task's bounded stage records, initial queue time, run timings and tool overview. Prompts and credentials are excluded. Follow cursor for further stage records; absent end records have no measured completion time. Delivery failures retain partial results in tasks.get; trace status does not prove semantic quality.",
    "operations.bind": "Bind one stable consumer and optional same-session managed coordinator thread. Rebinding changes the receiver; it grants no business tools. Internal coordinators receive coalesced control notifications. External clients use inbox or SDK events; a server cannot start an idle external chat turn.",
    "operations.inbox": "Compact durable summaries only. Keep consumer_id stable. Omit cursor to resume from the acknowledged position; limit defaults to 20, timeout=0..25 waits for a meaningful update without polling. Read selected event IDs, then ack the delivered cursor. Timeouts do not cancel work or start another Runtime.",
    "operations.read": "Read one channel event and report body after selecting its summary. Scope and references are checked; reports and replies never authorize extra tools.",
    "operations.reply": "Send idempotent guidance to the reporting Agent's existing thread. A suspended guidance request resumes the same task, preserving its checkpoints and logical operation keys. This is not a retry or confirmation that the underlying problem is resolved.",
    "operations.test": "Explicit diagnostic work uses one independent ordinary instance, saved Agent grants/model and the shared scheduler. May invoke the configured real model/tools. Stable keys recover the same test; query its task_id for actual results. No arbitrary shell, credential override or separate worker pool.",
    "instances.spawn": "Atomically create 1..128 independent Thread/Task instances in one session. Reuse Agent configurations. Stable key returns the original batch; changed input conflicts. agent_ids is a same-project managed pool, routing is round_robin or least_loaded; assignment is fixed at creation. max_concurrency bounds this batch within global and Agent budgets.",
    "instances.get": "Read ordered member IDs, states, counts and queue reasons; include_outputs=true reads full results. UNCERTAIN retains capacity and requires task reconciliation.",
    "instances.list": "Session-scoped batch summaries with cursor pagination; does not include full outputs or all conversation history.",
    "instances.result": "Wait for all instances to succeed, preserving input order. Nonblocking by default; bridge waits at most 30 seconds. Failure/cancellation/uncertainty requires inspecting individual task IDs; do not respawn with a new key.",
    "instances.cancel": "Request cancellation of unfinished batch members. Inspect confirmed_stopped and member tasks; never claim an uncertain or unsupported stop. Waiting parents must suspend, not occupy slots while blocking on children.",
    "workflows.create": "A workflow has one required, immutable project_id. Stores a draft; incomplete graphs may be saved. Stable idempotency_key; omitted graph creates start/end nodes. Use node_types for node configuration schemas.",
    "workflows.update": "Optimistic save requires expected_revision. Cannot change project_id. Running flows retain their revision snapshot. REVISION_CONFLICT requires reloading and merging edits.",
    "workflows.list": "Required project_id; lightweight metadata/counts with bounded cursor pagination. Use get for one full graph. Archived definitions excluded unless include_archived=true. deleted_only=true reads this project's workflow recycle bin.",
    "workflows.delete": "Requires confirmed=true after user authorization for this workflow. Recoverable deletion; unfinished or uncertain runs block it. Graph, project ownership and all run history are retained.",
    "workflows.restore": "Restores a deleted workflow to its previous active/archived state. Restore/reopen its project first if needed. Idempotent; does not reexecute runs.",
    "workflows.validate": "Checks DAG, start/end, true/false ports, upstream variable references, input schema and project Agents. Returns errors/warnings; does not execute anything.",
    "workflows.run": "Runs a saved revision in optional session_id (open, same project); omission creates a session. Multiple runs can share a session with independent ownership. Stable idempotency_key; expected_revision pins the definition. Inputs match the start JSON Schema; ordinary budgets and grants apply.",
    "workflows.get_run": "Durable node states, task IDs, full outputs and immutable graph snapshot. UNCERTAIN blocks downstream work until evidence-based task reconciliation; never automatically replay.",
    "workflows.result": "Nonblocking by default; timeout=0..30 seconds. WAIT_TIMEOUT leaves the workflow running.",
    "workflows.cancel": "Requests cancellation of executions owned by this run and its linked child instances. Other runs and ordinary session tasks continue. Inspect get_run until confirmed terminal; uncertain or unsupported stops are never reported as completed.",
    "workflows.archive": "Reversible archive; prevents new runs and edits. Existing runs and history are retained.",
    "workflows.resume_node": "Resume one waiting Agent node using genuine input and a stable idempotency_key. Same task, no reexecution of completed nodes.",
    "workflows.reconcile_node": "Evidence-based resolution of an UNCERTAIN Agent node. Same Reconciliation contract as tasks.reconcile; never fabricate stopped or external-action confirmation.",
    "projects.delete": "Requires confirmed=true after authorization for the specific project. Moves it and its sessions to the recycle bin; unfinished work blocks deletion. Local files and Agent configuration are retained.",
    "sessions.delete": "Requires confirmed=true after authorization for the specific session. Recoverable deletion; queued, running, waiting or uncertain tasks must finish first.",
    "projects.restore": "Restores this project and only the sessions deleted with it, preserving previous states and Agent settings.",
    "sessions.restore": "Restore the parent project first if it was deleted.",
    "sessions.list": "Deleted sessions are excluded by default. deleted_only=true lists the session recycle bin, excluding sessions inside deleted projects; include_deleted=true reads all states.",
    "projects.create": "Bind an existing absolute local folder. Use a stable idempotency_key.",
    "projects.adopt": "Move explicit session_ids and agent_ids together. All related history must be included and no work may remain unfinished.",
    "sessions.create": "Sessions belong to a project. Specify project_id whenever multiple projects exist.",
    "agents.list": "Optional project_id filters the Agent team; project membership is enforced by the runtime.",
    "tasks.get": "presentation=true returns a Markdown summary projection; default retains full collaboration data.",
    "tasks.progress": "Read the bounded live Markdown summary or final summary without returning internal peer messages.",
    "threads.conversation": "presentation=true shows summary Markdown; default retains full messages for Agent context.",
    "agents.test_model": "Send one short real model request with this Agent's saved configuration; may incur provider usage. No business prompt is sent.",
    "@configure": "changes is a partial RuntimeConfig; omitted settings are preserved. Active calls must fit the new budget.",
    "@agent_patch": "changes is a partial AgentConfig. Existing instructions, skills and grants are preserved; id cannot change. Local clients should serialize edits to the same Agent.",
    "agents.configure": "Full configuration replacement. For an existing Agent prefer agents.patch; credentials are write-only and omitted api_key preserves the saved key.",
    "threads.turn": "Starts a task on an existing thread. Optional extra fields are TaskSpec execution settings; identities and payload are inferred from this thread.",
    "tasks.workflow": "nodes: array of TaskSpec-like objects with unique key, thread_id, payload and optional depends_on (node keys or task IDs). session_id/idempotency_key are supplied by this operation.",
    "tasks.result": "Defaults to a nonblocking check (timeout=0); maximum bridge wait is 30 seconds. WAIT_TIMEOUT does not cancel the task.",
    "tasks.reconcile": "Only reconcile UNCERTAIN after evidence confirms execution stopped and external actions. Never invent evidence or automatically requeue.",
    "messages.send": "Delivery does not start an idle thread. External callers omit source_run_id/source_task_id; never impersonate an Agent run.",
    "mcp.tools": "Optional tool_name selects one tool schema. Without it, returns only tool names and descriptions; no upstream business tool is executed.",
    "mcp.call": "Executes one real upstream tool. Keep consumer_id stable per conversation. Reuse idempotency_key only for the identical operation; never replay an uncertain action.",
    "mcp.configure": "Full server configuration; read the existing server first. secret_values.env/headers replace only that map; omit to preserve, {} to clear.",
    "sessions.close": "drain waits for accepted work; cancel requests cancellation. Closing does not erase history.",
    "@upload_file": "Uploads one user-authorized absolute local file; does not send it to an external MCP service.",
    "@download_file": "Writes one artifact to an absolute path outside source repositories; existing files require overwrite=true.",
    "@read_result": "Reads a large prior result from this MCP process's expiring memory cache. pointer is an RFC 6901 JSON Pointer. Expiry is not permission to replay a mutation.",
}


def signature(method):
    owner, name = method.split(".", 1)
    return inspect.signature(getattr(OWNERS[owner], name))


def parameter_defaults(method):
    if method.startswith("@"):
        return dict(EXTRA_PARAMS[method])
    return {
        p.name: (... if p.default is inspect.Parameter.empty else p.default)
        for p in signature(method).parameters.values()
        if p.name not in {"self", "caller_run_id"} and p.kind != p.VAR_KEYWORD
    }


class ResultCache:
    def __init__(self, *, output_chars=8000, max_bytes=8 * 1024 * 1024, ttl=600):
        self.output_chars, self.max_bytes, self.ttl = output_chars, max_bytes, ttl
        self.entries = OrderedDict()

    def _expire(self):
        now = time.monotonic()
        for key in list(self.entries):
            if self.entries[key][0] <= now:
                del self.entries[key]

    def pack(self, value):
        encoded = models.json_dump(value)
        if len(encoded) <= self.output_chars:
            return {"ok": True, "result": json.loads(encoded)}
        self._expire()
        size = len(encoded.encode("utf-8"))
        if size > self.max_bytes:
            return {
                "ok": True,
                "result_omitted": True,
                "characters": len(encoded),
                "reason": "Result exceeds bridge cache capacity; use a narrower read or a file export. Do not repeat a mutation.",
            }
        while (
            self.entries
            and sum(item[2] for item in self.entries.values()) + size > self.max_bytes
        ):
            self.entries.popitem(last=False)
        key = "result_" + uuid.uuid4().hex
        self.entries[key] = (time.monotonic() + self.ttl, encoded, size)
        return {
            "ok": True,
            "result_ref": key,
            "characters": len(encoded),
            "preview": encoded[:500],
            "expires_in_seconds": self.ttl,
            "next": {
                "area": "control",
                "action": "read_result",
                "params": {"result_ref": key},
            },
            "untrusted_data": True,
        }

    def read(self, result_ref, pointer="", offset=0, limit=4000):
        self._expire()
        require(
            result_ref in self.entries,
            "RESULT_EXPIRED",
            "Result is unavailable. Do not repeat a mutation to reconstruct it.",
        )
        require(
            isinstance(pointer, str) and (not pointer or pointer.startswith("/")),
            "INVALID_REQUEST",
            "Use a JSON Pointer beginning with /.",
        )
        value = json.loads(self.entries[result_ref][1])
        try:
            for token in pointer.split("/")[1:]:
                token = token.replace("~1", "/").replace("~0", "~")
                value = (
                    value[int(token)]
                    if isinstance(value, list) and token.isdecimal()
                    else value[token]
                )
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeFault(
                "NOT_FOUND", "Result pointer does not identify a value"
            ) from None
        require(
            type(offset) is int
            and offset >= 0
            and type(limit) is int
            and 1 <= limit <= 6000,
            "INVALID_REQUEST",
            "offset must be nonnegative; limit must be 1..6000",
        )
        text = models.json_dump(value)
        end = min(offset + limit, len(text))
        return {
            "result_ref": result_ref,
            "pointer": pointer,
            "text": text[offset:end],
            "next_offset": end if end < len(text) else None,
            "characters": len(text),
            "untrusted_data": True,
        }


class CompactBridge:
    def __init__(self, invoke, gateway=None):
        self.invoke, self.gateway = invoke, gateway
        self.cache = ResultCache()

    def guide(self, area="overview", action=None):
        if area == "overview":
            require(
                action is None,
                "INVALID_REQUEST",
                "Select an area before selecting an action",
            )
            return {
                "name": PRODUCT_NAME,
                "interface": "compact-v1",
                "skill": "$" + SKILL_NAME,
                "areas": {name: info[0] for name, info in AREAS.items()},
                "workflow": "Read the main skill, load one matching workflow, inspect only that action, then call it. No batch tool or full protocol dump.",
                "scope": "A connected MCP bridge reuses the existing HTTP host. Skill instructions guide behavior; runtime grants enforce Agent tool access.",
            }
        require(area in AREAS, "INVALID_REQUEST", "Unknown area; request the overview")
        if area == "development":
            if action is None:
                return {
                    "area": area,
                    "schemas": list(DEV_MODELS),
                    "adapter_methods": [
                        "describe",
                        "execute",
                        "cancel",
                        "probe",
                        "aclose",
                    ],
                    "guide": "Read workflows/develop.md. Use agent.stdio_sdk.run_agent for process execution, register kind=process, then submit an acceptance task.",
                }
            require(
                action in DEV_MODELS, "INVALID_REQUEST", "Unknown development schema"
            )
            return {"area": area, "schema": DEV_MODELS[action].model_json_schema()}
        if area == "maintenance":
            require(
                action is None,
                "INVALID_REQUEST",
                "Maintenance is a local workflow, not a shell gateway",
            )
            return {
                "area": area,
                "workflow": "workflows/maintenance.md",
                "operations": [
                    "Start-Workspace.cmd / Start-Workspace.ps1",
                    "CLI backup with --data-dir and --output",
                    "SDK/protocol verification",
                    "bounded local log inspection",
                ],
                "boundary": "Do not start a second Runtime for an active data directory. No arbitrary shell, process kill, restore, or migration operation is exposed here.",
            }
        actions = AREAS[area][1]
        if action is None:
            return {
                "area": area,
                "description": AREAS[area][0],
                "actions": list(actions),
            }
        require(action in actions, "INVALID_REQUEST", "Unknown action for this area")
        method = actions[action]
        parameters, schemas = [], {}
        defaults = parameter_defaults(method)
        if method == "mcp.tools":
            defaults["tool_name"] = None
        for name, default in defaults.items():
            entry = {"name": name, "required": default is ...}
            if default is not ...:
                entry["default"] = (
                    0
                    if method == "tasks.result" and name == "timeout"
                    else (
                        (20 if method == "operations.inbox" else 25)
                        if name == "limit" and not method.startswith("@")
                        else default
                    )
                )
            model = PARAM_MODELS.get((method, name))
            if model:
                entry["schema"] = model.__name__
                schemas[model.__name__] = model.model_json_schema()
            parameters.append(entry)
        if method in {"tasks.workflow", "threads.turn"}:
            schemas["TaskSpec"] = models.TaskSpec.model_json_schema()
        if method in {"@configure", "@agent_patch"}:
            model = (
                models.RuntimeConfig if method == "@configure" else models.AgentConfig
            )
            schemas[model.__name__] = model.model_json_schema()
        return {
            "area": area,
            "action": action,
            "parameters": parameters,
            **({"schemas": schemas} if schemas else {}),
            "notes": NOTES.get(
                method,
                "Use IDs from the current workspace. Read only the needed records.",
            ),
        }

    async def call(self, area, action, params):
        require(isinstance(params, dict), "INVALID_REQUEST", "params must be an object")
        require(
            area in AREAS and action in AREAS[area][1],
            "METHOD_NOT_FOUND",
            "Choose one documented area/action",
        )
        require(
            len(models.json_dump(params).encode("utf-8")) <= 1024 * 1024,
            "PAYLOAD_TOO_LARGE",
            "Parameters exceed 1 MiB",
        )
        method = AREAS[area][1][action]
        defaults = parameter_defaults(method)
        allowed = set(defaults)
        if method == "threads.turn":
            allowed |= set(models.TaskSpec.model_fields) - {
                "session_id",
                "agent_id",
                "thread_id",
                "payload",
            }
        if method == "mcp.tools":
            allowed.add("tool_name")
        require(
            not (set(params) - allowed),
            "INVALID_REQUEST",
            "Unexpected parameters; inspect this action with platform_guide",
        )
        require(
            all(name in params for name, default in defaults.items() if default is ...),
            "INVALID_REQUEST",
            "Missing parameters; inspect this action with platform_guide",
        )
        params = dict(params)
        if "limit" in defaults and not method.startswith("@"):
            params.setdefault("limit", 20 if method == "operations.inbox" else 25)
        if method in {"tasks.result", "workflows.result", "instances.result"}:
            params.setdefault("timeout", 0)
            require(
                type(params["timeout"]) in {int, float}
                and 0 <= params["timeout"] <= 30,
                "INVALID_REQUEST",
                "timeout must be 0..30 seconds; waiting does not cancel work",
            )
        if method == "@read_result":
            return {"ok": True, "result": self.cache.read(**params)}
        if method == "@agent_patch":
            require(
                isinstance(params["changes"], dict) and "id" not in params["changes"],
                "INVALID_REQUEST",
                "changes must be an object and cannot change id",
            )
            existing = await self.invoke(
                "agents.describe", {"agent_id": params["agent_id"]}
            )
            current = {
                k: v
                for k, v in existing.items()
                if k in models.AgentConfig.model_fields
            }
            updated = models.AgentConfig.model_validate(
                {**current, **params["changes"]}
            )
            value = await self.invoke(
                "agents.configure", {"config": updated.model_dump(mode="json")}
            )
        elif method.startswith("@"):
            require(
                self.gateway is not None,
                "UNSUPPORTED_CAPABILITY",
                "This action requires the connected HTTP gateway",
            )
            value = await self.gateway(method, params)
        else:
            selected_tool = (
                params.pop("tool_name", None) if method == "mcp.tools" else None
            )
            value = await self.invoke(method, params)
            if method == "mcp.tools":
                if selected_tool:
                    tools = [
                        t
                        for t in value.get("tools", [])
                        if t.get("name") == selected_tool
                    ]
                    require(
                        tools,
                        "NOT_FOUND",
                        "The named tool is not exposed by this service",
                    )
                    value = {**value, "tools": tools}
                else:
                    value = {
                        **value,
                        "tools": [
                            {
                                "name": t["name"],
                                "description": t.get("description", "")[:600],
                            }
                            for t in value.get("tools", [])
                        ],
                        "next": "Use mcp.tools with tool_name to read one input schema.",
                    }
        return self.cache.pack(value)
