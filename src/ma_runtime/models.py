"""Versioned public types. No provider credentials are accepted in task types."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = "1.0"
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"})


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
        default=lambda v: v.model_dump(mode="json"),
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(json_dump(value).encode("utf-8")).hexdigest()


class View(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    schema_version: str = PROTOCOL_VERSION

    @field_validator("schema_version")
    @classmethod
    def compatible_schema(cls, value):
        if value.split(".")[0] != "1":
            raise ValueError("Schema major version does not match")
        return value

    def __getitem__(self, name):
        return getattr(self, name)

    def get(self, name, default=None):
        return getattr(self, name, default)

    def keys(self):
        return type(self).model_fields.keys()


class TaskHandle(Model):
    id: str
    state: str
    reused: bool = False


class MessageHandle(Model):
    id: str
    state: str
    reused: bool = False


class RuntimeConfig(Model):
    max_concurrency: int = Field(default=16, ge=1, le=256)
    reserved_external_slots: int = Field(default=1, ge=0)
    max_queued_tasks: int = Field(default=1000, ge=1)
    cancel_grace_seconds: float = Field(default=10, gt=0, le=120)
    probe_timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_message_bytes: int = Field(default=65536, ge=1024)
    max_event_payload_bytes: int = Field(default=16384, ge=1024)
    max_artifact_bytes: int = Field(default=52428800, ge=1024)
    subscriber_buffer_size: int = Field(default=256, ge=8, le=4096)
    strict_concurrency: bool = True

    @model_validator(mode="after")
    def budget(self):
        if self.reserved_external_slots >= self.max_concurrency:
            raise ValueError(
                "reserved_external_slots must be smaller than max_concurrency"
            )
        return self


class ResourceRequirement(Model):
    key: str = Field(min_length=1, max_length=512)
    mode: Literal["SHARED", "EXCLUSIVE"] = "EXCLUSIVE"


class UsePolicy(Model):
    """Task-scoped restrictions; never grants capabilities."""

    mode: Literal["auto", "none", "required"] = "auto"
    allowed: tuple[str, ...] | None = Field(default=None, max_length=96)
    required: tuple[str, ...] = Field(default=(), max_length=96)

    @model_validator(mode="after")
    def consistent(self):
        if self.mode == "none" and (self.required or self.allowed):
            raise ValueError("none cannot select or require capabilities")
        if self.allowed is not None and not set(self.required) <= set(self.allowed):
            raise ValueError("required must be a subset of allowed")
        for values in (self.allowed or (), self.required):
            if any(not value or len(value) > 256 for value in values):
                raise ValueError("Capability identifiers must contain 1..256 characters")
            if len(set(values)) != len(values):
                raise ValueError("Capability identifiers must be unique")
        return self

    def permits(self, name):
        return self.mode != "none" and (self.allowed is None or name in self.allowed)


class ResultPolicy(Model):
    """Deterministic delivery requirements; semantic review belongs to the caller."""

    kind: Literal["auto", "text", "structured", "artifacts"] = "auto"
    output_schema: dict | None = None
    required_artifacts: tuple[str, ...] = Field(default=(), max_length=16)
    min_artifacts: int = Field(default=0, ge=0, le=64)
    required_items: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("required_artifacts", "required_items")
    @classmethod
    def unique_names(cls, value):
        if len(set(value)) != len(value) or any(not name.strip() or len(name) > 256 for name in value):
            raise ValueError("Result requirement names must be unique and contain 1..256 characters")
        return value

    @field_validator("output_schema")
    @classmethod
    def valid_schema(cls, value):
        if value is not None:
            from .result_validation import output_validator
            output_validator(json_dump(value))
        return value


class ContinuationPolicy(Model):
    """Additional work is explicitly requested and bounded across the task."""
    max_runs: int = Field(default=2, ge=0, le=3)
    max_model_calls: int = Field(default=4, ge=1, le=12)
    max_output_tokens: int = Field(default=8192, ge=128, le=131072)
    timeout_seconds: float = Field(default=120, gt=0, le=900)


class TaskSpec(Model):
    schema_version: str = PROTOCOL_VERSION
    session_id: str
    agent_id: str
    thread_id: str
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)
    depends_on: tuple[str, ...] = ()
    dependency_policy: Literal["ALL_SUCCEEDED", "ALL_TERMINAL"] = "ALL_SUCCEEDED"
    resources: tuple[ResourceRequirement, ...] = ()
    deadline_at_ms: int | None = None
    attempt_timeout_s: float = Field(default=900, gt=0, le=86400)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_policy: Literal["NEVER", "SAFE_ONLY"] = "NEVER"
    metadata: dict = Field(default_factory=dict)
    tool_policy: UsePolicy = Field(default_factory=UsePolicy)
    skill_policy: UsePolicy = Field(default_factory=UsePolicy)
    result_policy: ResultPolicy = Field(default_factory=ResultPolicy)
    context_budget_tokens: int | None = Field(default=None, ge=512, le=1048576)
    context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    continuation_policy: ContinuationPolicy | None = None

    @field_validator("resources")
    @classmethod
    def unique_resources(cls, value):
        if len({v.key for v in value}) != len(value):
            raise ValueError("Resource keys must be unique")
        return value


class InstanceBatchSpec(Model):
    session_id: str
    agent_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    payloads: tuple[dict, ...] = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    max_concurrency: int = Field(default=4, ge=1, le=256)
    routing: Literal["round_robin", "least_loaded"] = "round_robin"
    depends_on: tuple[str, ...] = ()
    attempt_timeout_s: float = Field(default=900, gt=0, le=86400)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_policy: Literal["NEVER", "SAFE_ONLY"] = "NEVER"
    tool_policy: UsePolicy = Field(default_factory=UsePolicy)
    skill_policy: UsePolicy = Field(default_factory=UsePolicy)
    result_policy: ResultPolicy = Field(default_factory=ResultPolicy)
    context_budget_tokens: int | None = Field(default=None, ge=512, le=1048576)
    context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    continuation_policy: ContinuationPolicy | None = None

    @field_validator("agent_ids")
    @classmethod
    def unique_agents(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Agent pool members must be unique")
        return value


class MessageSpec(Model):
    schema_version: str = PROTOCOL_VERSION
    session_id: str
    target_thread_id: str
    target_port: str = Field(default="input", min_length=1, max_length=64)
    message_type: str = Field(
        default="application.message", min_length=1, max_length=128
    )
    payload: dict = Field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_thread_id: str | None = None
    source_task_id: str | None = None
    source_run_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None


class CheckpointProposal(Model):
    mode: Literal["opaque_json", "native_ref"] = "opaque_json"
    format_version: str = "1.0"
    adapter_version: str = "1.0"
    ref: dict = Field(default_factory=dict)


class WaitCondition(Model):
    kind: Literal["tasks", "message", "input"]
    task_ids: tuple[str, ...] = ()
    policy: Literal["ALL_SUCCEEDED", "ALL_TERMINAL", "ALL_SETTLED"] = "ALL_SUCCEEDED"
    port: str = "input"
    match: dict = Field(default_factory=dict)
    wait_token: str | None = None
    deadline_at_ms: int | None = None


class TaskResult(Model):
    output: dict = Field(default_factory=dict)
    summary_markdown: str = ""
    artifact_ids: tuple[str, ...] = ()
    metadata: dict = Field(default_factory=dict)


class AdapterEvent(Model):
    type: Literal["progress", "log", "usage", "completed", "suspended"]
    payload: dict = Field(default_factory=dict)
    result: TaskResult | None = None
    checkpoint: CheckpointProposal | None = None
    wait: WaitCondition | None = None
    applied_message_ids: tuple[str, ...] = ()
    outgoing_messages: tuple[MessageSpec, ...] = ()
    local_seq: int | None = None

    @model_validator(mode="after")
    def boundary(self):
        if self.type == "completed" and self.result is None:
            raise ValueError("completed requires result")
        if self.type == "suspended" and (self.checkpoint is None or self.wait is None):
            raise ValueError("suspended requires checkpoint and wait")
        return self


class AgentDescriptor(Model):
    agent_id: str
    adapter_version: str = "1.0"
    protocol_version: str = PROTOCOL_VERSION
    execution_mode: Literal["in_process", "subprocess", "external"] = "in_process"
    max_concurrency: int = Field(default=1, ge=1, le=64)
    checkpoint_mode: Literal["none", "opaque_json", "native_ref"] = "none"
    checkpoint_format_version: str = "1.0"
    supports_suspend: bool = False
    supports_fork: bool = False
    cancellation: Literal["cooperative", "terminate_owned_process", "unsupported"] = (
        "cooperative"
    )
    message_receipts: Literal["next_turn_ack", "unsupported"] = "next_turn_ack"
    tool_mediation: Literal["full", "partial", "none"] = "full"
    supports_probe: bool = True
    nested_execution: Literal["none", "runtime_mediated", "unmanaged"] = "none"
    ports: tuple[str, ...] = ("input",)


class AgentSkill(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2048)
    content: str = Field(default="", max_length=65536)
    enabled: bool = True
    resources: dict[str, str] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    revision: str = ""

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value):
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("SKILL.md cannot exceed 64 KiB")
        return value

    @field_validator("resources")
    @classmethod
    def valid_resources(cls, value):
        from pathlib import PurePosixPath

        if len(value) > 32 or sum(len(v.encode()) for v in value.values()) > 262144:
            raise ValueError("Skill resources exceed their bounded text allowance")
        for name in value:
            path = PurePosixPath(name)
            if (
                not name
                or "\\" in name
                or ":" in name
                or path.is_absolute()
                or ".." in path.parts
                or name == "SKILL.md"
            ):
                raise ValueError(
                    "Skill resources must use relative paths below the skill"
                )
        return value


class SkillBinding(Model):
    skill_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)


class MCPBinding(Model):
    server_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    # None grants the server's available tools; an empty tuple grants no tools.
    tools: tuple[str, ...] | None = None


class MCPServerConfig(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2048)
    source_directory: str = Field(default="", max_length=4096)
    transport: Literal["stdio", "streamable-http", "sse"] = "stdio"
    command: str = Field(default="", max_length=4096)
    args: tuple[str, ...] = ()
    cwd: str = Field(default="", max_length=4096)
    url: str = Field(default="", max_length=4096)
    enabled: bool = True
    expose_to_codex: bool = True
    session_scope: Literal["consumer", "shared"] = "consumer"
    allowed_tools: tuple[str, ...] | None = None
    startup_timeout_seconds: float = Field(default=30, gt=0, le=120)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    max_connections: int = Field(default=16, ge=1, le=64)
    max_output_bytes: int = Field(default=524288, ge=1024, le=4194304)
    persist_results: bool = False

    @model_validator(mode="after")
    def valid_transport(self):
        if len(self.args) > 128 or sum(len(a) for a in self.args) > 32768:
            raise ValueError("MCP command arguments are too large")
        if self.transport == "stdio":
            if not self.command.strip() or any(c in self.command for c in "\r\n\0"):
                raise ValueError("STDIO requires a valid executable command")
            if self.url:
                raise ValueError("A STDIO server cannot also declare an HTTP URL")
        else:
            parts = urlsplit(self.url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.fragment
                or parts.query
            ):
                raise ValueError(
                    "MCP URL must be HTTP(S), without embedded credentials or query parameters"
                )
            if self.command or self.args or self.cwd:
                raise ValueError("An HTTP server cannot also declare a process command")
        return self


WORKBENCH_TOOLS = ("read_file", "search_files", "write_file", "edit_file", "exec_command", "read_image", "publish_artifact")


class WorkbenchConfig(Model):
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    permission_profile: Literal["read-only", "workspace-write", "trusted-local"] = "read-only"
    tools: tuple[Literal["read_file", "search_files", "write_file", "edit_file", "exec_command", "read_image", "publish_artifact"], ...] = ()


class GraphAdapterConfig(Model):
    managed_context: bool = False
    recursion_limit: int = Field(default=24, ge=1, le=256)
    graph_version: str = Field(default="1.0", min_length=1, max_length=128)


class EnvironmentConfig(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    project_id: str
    runner: Literal["local", "isolated"] = "local"
    enabled: bool = True
    project_write: bool = False
    network: Literal["inherit", "none"] = "inherit"
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    output_bytes: int = Field(default=16384, ge=1024, le=65536)
    programs: dict[str, str] = Field(default_factory=dict)
    variables: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)


class AgentConfig(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    project_id: str | None = None
    kind: Literal["llm", "mock", "process", "external", "environment"] = "llm"
    enabled: bool = True
    llm_required: bool = False
    base_url: str = ""
    model: str = Field(default="", max_length=256)
    system_prompt: str = Field(default="", max_length=16384)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=4096, ge=1, le=131072)
    context_window_tokens: int | None = Field(default=None, ge=1024, le=2097152)
    context_budget_tokens: int = Field(default=48000, ge=512, le=1048576)
    token_parameter: Literal["max_completion_tokens", "max_tokens"] = (
        "max_completion_tokens"
    )
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    collaboration: bool = True
    command: tuple[str, ...] = ()
    process_descriptor: AgentDescriptor | None = None
    agent_md: str = Field(default="", max_length=65536)
    skills: tuple["AgentSkill", ...] = ()
    skill_bindings: tuple[SkillBinding, ...] = ()
    mcp_bindings: tuple["MCPBinding", ...] = ()
    workbench: WorkbenchConfig | None = None

    @field_validator("agent_md")
    @classmethod
    def bounded_instructions(cls, value):
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("AGENT.md cannot exceed 64 KiB")
        return value

    @model_validator(mode="after")
    def unique_profile_entries(self):
        if self.context_window_tokens is not None and self.context_window_tokens <= (self.max_output_tokens or 4096) + 1024:
            raise ValueError("Context window must exceed the output reserve and safety margin")
        if self.process_descriptor and (
            self.kind != "process"
            or self.process_descriptor.agent_id != self.id
            or self.process_descriptor.execution_mode != "subprocess"
        ):
            raise ValueError(
                "Process capability descriptor must identify this subprocess Agent"
            )
        if len({s.id for s in self.skills}) != len(self.skills):
            raise ValueError("Skill IDs must be unique within an agent")
        linked = [binding.skill_id for binding in self.skill_bindings]
        if len(set(linked)) != len(linked) or set(linked) & {s.id for s in self.skills}:
            raise ValueError("Linked and embedded Skill IDs must be unique")
        if len(linked) + len(self.skills) > 32:
            raise ValueError("An Agent supports up to 32 linked and embedded skills")
        if len({b.server_id for b in self.mcp_bindings}) != len(self.mcp_bindings):
            raise ValueError("MCP server bindings must be unique within an agent")
        if len(self.skills) > 32 or len(self.mcp_bindings) > 32:
            raise ValueError("An agent supports up to 32 skills and 32 MCP bindings")
        if len(json_dump([s.model_dump() for s in self.skills]).encode()) > 524288:
            raise ValueError("Agent skill documents exceed 512 KiB")
        return self

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        if not value:
            return value
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "Base URL must be HTTP(S), without credentials, query or fragment"
            )
        return value.rstrip("/")


class LLMRequest(Model):
    messages: list[dict] = Field(min_length=1, max_length=200)
    tools: list[dict] | None = Field(default=None, max_length=64)
    include_instructions: bool = True
    emit_progress: bool = False
    tool_choice: str | dict | None = None
    allow_incomplete: bool = False

    @model_validator(mode="after")
    def bounded_model_input(self):
        if len(json_dump(self).encode("utf-8")) > 524288:
            raise ValueError("Model input exceeds 512 KiB")
        for message in self.messages:
            if message.get("role") not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }:
                raise ValueError("Unsupported model message role")
        for tool in self.tools or []:
            if tool.get("type") != "function" or not isinstance(
                tool.get("function"), dict
            ):
                raise ValueError("Model tools must be function definitions")
        return self


class LLMResponse(Model):
    call_id: str = ""
    model: str
    text: str = ""
    tool_calls: list[dict] = Field(default_factory=list)
    finish_reason: str
    usage: dict = Field(default_factory=dict)


class ExecutionRequest(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    task_id: str
    run_id: str
    thread_id: str
    agent_id: str
    project_id: str = "default"
    project_folder: str = ""
    expected_thread_revision: int
    conversation_cutoff: int = 0
    continuation: dict | None = None
    payload: dict
    messages: list[dict] = Field(default_factory=list)
    conversation: list[dict] = Field(default_factory=list)
    dependency_results: dict = Field(default_factory=dict)
    resume_ref: dict | None = None
    resume_input: dict | None = None
    deadline_at_ms: int | None = None
    attempt: int = 1
    agent_md: str = ""
    skills: tuple["AgentSkill", ...] = ()
    mcp_bindings: tuple["MCPBinding", ...] = ()
    tool_policy: UsePolicy = Field(default_factory=UsePolicy)
    skill_policy: UsePolicy = Field(default_factory=UsePolicy)
    result_policy: ResultPolicy = Field(default_factory=ResultPolicy)
    context_budget_tokens: int | None = Field(default=None, ge=512, le=1048576)
    context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    operations_consumer_id: str = ""
    workbench: dict | None = None


class ProbeResult(Model):
    state: Literal["NOT_STARTED", "RUNNING", "COMPLETED", "STOPPED", "UNKNOWN"]
    confirmed_stopped: bool = False
    safe_to_retry: bool = False
    boundary: AdapterEvent | None = None
    evidence: str = ""


class CancelReceipt(Model):
    accepted: bool
    confirmed_stopped: bool = False
    native_ref: dict = Field(default_factory=dict)


class Reconciliation(Model):
    resolution: Literal["SUCCEEDED", "FAILED", "CANCELLED", "QUEUED"]
    evidence: str = Field(min_length=8, max_length=4096)
    confirmed_stopped: bool
    external_actions_confirmed: bool
    safe_to_retry: bool = False
    result: TaskResult | None = None


class SnapshotModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionInfo(SnapshotModel):
    id: str
    state: str
    title: str
    project_id: str = "default"
    metadata: dict
    close_mode: str | None = None
    created_at: int
    closed_at: int | None = None
    position: int | None = None


class ThreadSnapshot(SnapshotModel):
    id: str
    session_id: str
    agent_id: str
    title: str
    adapter_snapshot: AgentDescriptor
    revision: int
    owner_task_id: str | None = None
    active_run_id: str | None = None
    latest_checkpoint_id: str | None = None
    blocked_reason: str | None = None
    fork_of_thread_id: str | None = None
    created_at: int


class RunSnapshot(SnapshotModel):
    id: str
    task_id: str
    attempt_no: int
    host_instance_id: str
    expected_revision: int
    state: str
    adapter_snapshot: AgentDescriptor
    adapter_run_ref: dict | None = None
    held_resources: list[ResourceRequirement]
    boundary_seq: int | None = None
    boundary_hash: str | None = None
    boundary_receipt: dict | None = None
    started_at: int
    ended_at: int | None = None
    error: dict | None = None


class TaskSnapshot(SnapshotModel):
    id: str
    session_id: str
    thread_id: str
    spec: TaskSpec
    state: str
    queue_reason: str | None = None
    idempotency_key: str
    request_hash: str
    result: TaskResult | None = None
    retry_of_task_id: str | None = None
    cancel_requested_at: int | None = None
    wait: WaitCondition | None = None
    resume_input: dict | None = None
    resume_key: str | None = None
    resume_hash: str | None = None
    attempt_count: int
    continuation_runs: int = 0
    continuation: dict | None = None
    not_before: int
    error: dict | None = None
    created_at: int
    updated_at: int
    runs: list[RunSnapshot] = Field(default_factory=list)
    current_run: RunSnapshot | None = None
    execution_evidence: list[dict] = Field(default_factory=list)
    position: int | None = None


class MessageEnvelope(MessageSpec):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: str


class MessageSnapshot(SnapshotModel):
    id: str
    session_id: str
    source_key: str
    source_thread_id: str | None = None
    target_thread_id: str
    target_port: str
    envelope: MessageEnvelope
    state: str
    target_run_id: str | None = None
    applied_checkpoint_id: str | None = None
    idempotency_key: str
    request_hash: str
    created_at: int
    updated_at: int
    position: int | None = None


class ArtifactRef(SnapshotModel):
    id: str
    session_id: str
    sha256: str
    size: int
    name: str
    media_type: str
    state: str
    metadata: dict
    created_at: int


class ConversationEntry(SnapshotModel):
    seq: int
    id: str
    session_id: str
    thread_id: str
    task_id: str | None = None
    run_id: str | None = None
    role: str
    content: str
    summary_markdown: str = ""
    metadata: dict
    created_at: int


class ProjectInfo(SnapshotModel):
    id: str
    name: str
    local_folder: str
    state: str
    created_at: int
    updated_at: int


class SessionSnapshot(SnapshotModel):
    session: SessionInfo
    threads: list[ThreadSnapshot]
    tasks: list[TaskSnapshot]
    messages: list[MessageSnapshot]
    artifacts: list[ArtifactRef]
    cursor: int


class RuntimeEvent(SnapshotModel):
    seq: int | None = None
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    session_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    type: str
    created_at: int = Field(default_factory=now_ms)
    payload: dict = Field(default_factory=dict)
    durable: bool


class PageEnvelope(SnapshotModel):
    items: list[Any]
    cursor: str | int | None = None
    has_more: bool = False


class WorkflowPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: float = Field(default=0, ge=-100000, le=100000)
    y: float = Field(default=0, ge=-100000, le=100000)


class WorkflowViewport(WorkflowPosition):
    zoom: float = Field(default=1, ge=0.2, le=2)


class WorkflowStartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})


class WorkflowAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(default="", max_length=256)
    payload: dict = Field(default_factory=lambda: {"input": "{{inputs.input}}"})
    attempt_timeout_s: float = Field(default=900, gt=0, le=86400)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_policy: Literal["NEVER", "SAFE_ONLY"] = "NEVER"
    tool_policy: UsePolicy = Field(default_factory=UsePolicy)
    skill_policy: UsePolicy = Field(default_factory=UsePolicy)
    result_policy: ResultPolicy = Field(default_factory=ResultPolicy)
    context_budget_tokens: int | None = Field(default=None, ge=512, le=1048576)
    context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    continuation_policy: ContinuationPolicy | None = None


class WorkflowMapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_ids: tuple[str, ...] = Field(default=(), max_length=32)
    items: Any = "{{inputs.items}}"
    payload: dict = Field(default_factory=lambda: {"input": "{{item}}"})
    max_concurrency: int = Field(default=4, ge=1, le=256)
    routing: Literal["round_robin", "least_loaded"] = "round_robin"
    attempt_timeout_s: float = Field(default=900, gt=0, le=86400)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_policy: Literal["NEVER", "SAFE_ONLY"] = "NEVER"
    tool_policy: UsePolicy = Field(default_factory=UsePolicy)
    skill_policy: UsePolicy = Field(default_factory=UsePolicy)
    result_policy: ResultPolicy = Field(default_factory=ResultPolicy)
    context_budget_tokens: int | None = Field(default=None, ge=512, le=1048576)
    context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    continuation_policy: ContinuationPolicy | None = None


class WorkflowConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: Any = "{{inputs.input}}"
    operator: Literal["equals", "not_equals", "contains", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty"] = "equals"
    right: Any = ""


class WorkflowTemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(default="{{inputs.input}}", max_length=65536)


class WorkflowEndConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: dict = Field(default_factory=lambda: {"text": "{{inputs.input}}"})


WORKFLOW_CONFIGS = {
    "start": WorkflowStartConfig,
    "agent": WorkflowAgentConfig,
    "map": WorkflowMapConfig,
    "condition": WorkflowConditionConfig,
    "template": WorkflowTemplateConfig,
    "end": WorkflowEndConfig,
}


class WorkflowNode(Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: Literal["start", "agent", "map", "condition", "template", "end"]
    title: str = Field(default="", max_length=120)
    position: WorkflowPosition = Field(default_factory=WorkflowPosition)
    config: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def node_config(self):
        normalized = WORKFLOW_CONFIGS[self.type].model_validate(self.config).model_dump(mode="json")
        object.__setattr__(self, "config", normalized)
        return self


class WorkflowEdge(Model):
    id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    source_handle: Literal["default", "true", "false"] = "default"


class WorkflowGraph(Model):
    nodes: tuple[WorkflowNode, ...] = Field(default=(), max_length=100)
    edges: tuple[WorkflowEdge, ...] = Field(default=(), max_length=400)
    viewport: WorkflowViewport = Field(default_factory=WorkflowViewport)


class WorkflowInfo(SnapshotModel):
    id: str
    project_id: str
    name: str
    description: str
    state: str
    revision: int
    graph: WorkflowGraph
    created_at: int
    updated_at: int
    validation: dict = Field(default_factory=dict)
    deleted_at: int | None = None


class WorkflowSummary(SnapshotModel):
    id: str
    project_id: str
    name: str
    description: str
    state: str
    revision: int
    node_count: int
    edge_count: int
    deleted_at: int | None = None
    created_at: int
    updated_at: int


class WorkflowRunHandle(Model):
    id: str
    workflow_id: str
    project_id: str
    session_id: str
    revision: int
    state: str
    reused: bool = False


class WorkflowNodeRun(SnapshotModel):
    run_id: str
    node_id: str
    type: str
    title: str
    state: str
    task_id: str | None = None
    batch_id: str | None = None
    instances: list[dict] = Field(default_factory=list)
    inputs: Any = None
    output: Any = None
    summary_markdown: str = ""
    error: dict | None = None
    branch: str | None = None
    started_at: int | None = None
    ended_at: int | None = None


class WorkflowRunSnapshot(SnapshotModel):
    id: str
    workflow_id: str
    project_id: str
    session_id: str
    revision: int
    graph: WorkflowGraph
    inputs: dict
    state: str
    result: dict | None = None
    summary_markdown: str = ""
    error: dict | None = None
    cancel_requested_at: int | None = None
    created_at: int
    updated_at: int
    ended_at: int | None = None
    nodes: list[WorkflowNodeRun] = Field(default_factory=list)
