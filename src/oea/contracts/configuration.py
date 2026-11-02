"""Shared version-three Agent configuration; construction performs no I/O."""

from __future__ import annotations

import os
from pathlib import Path
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = "3.0"
# Encoded image data URIs share one request envelope with text and tools.
MODEL_REQUEST_TEXT_BYTES = 8 * 1024 * 1024
MODEL_REQUEST_IMAGE_BYTES = 32 * 1024 * 1024
MODEL_REQUEST_MAX_BYTES = MODEL_REQUEST_TEXT_BYTES + MODEL_REQUEST_IMAGE_BYTES
MODEL_SSE_EVENT_MAX_BYTES = 1024 * 1024
MODEL_OUTPUT_MIN_BYTES = 2 * 1024 * 1024
MODEL_OUTPUT_BYTES_PER_TOKEN = 128  # Local retention policy, not a provider capability.
MODEL_IMAGE_MAX_BYTES = 5 * 1024 * 1024
MODEL_IMAGE_MAX_PIXELS = 20_000_000


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ModelPricing(Configuration):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    revision: str = Field(min_length=1, max_length=128)
    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    cache_read_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_per_million: Decimal | None = Field(default=None, ge=0)
    cache_upper_bound_per_million: Decimal | None = Field(default=None, ge=0)


class Model(Configuration):
    base_url: str = ""
    model: str = ""
    credential_ref: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    max_output_tokens: int = Field(default=4096, ge=1, le=131072)
    context_window_tokens: int | None = Field(default=None, ge=1024)
    token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    usage_format: Literal["openai", "anthropic"] = "openai"
    pricing: ModelPricing | None = None

    @field_validator("credential_ref")
    @classmethod
    def credential_name(cls, value):
        if value is not None and (not value.startswith(("env:", "vault:")) or not value.partition(":")[2] or len(value) > 256):
            raise ValueError("Credentials use a named env: or vault: reference")
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("Model endpoint must be HTTP(S) without credentials, query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def effective_window(self):
        if self.context_window_tokens is not None and self.context_window_tokens <= self.max_output_tokens + 1024:
            raise ValueError("Model window must exceed output reserve and safety margin")
        return self

    @classmethod
    def from_env(cls, *, prefix="OPENAI", **options):
        """Read non-secret settings and bind a credential name, without copying it."""
        values = {"base_url": os.environ.get(prefix + "_BASE_URL", "https://api.openai.com/v1"),
                  "model": os.environ.get(prefix + "_MODEL", ""), "credential_ref": "env:" + prefix + "_API_KEY"}
        return cls(**{**values, **options})


class Instructions(Configuration):
    system: str = Field(default="", max_length=16384)
    agent_md: str = Field(default="", max_length=65536)


class MCPBinding(Configuration):
    server_id: str = Field(min_length=1)
    tools: tuple[str, ...] | None = None


class Tools(Configuration):
    loading: Literal["deferred", "eager"] = "deferred"
    schema_budget_tokens: int | None = Field(default=None, ge=1)
    local: tuple[Literal["exec_command", "write_stdin", "apply_patch", "view_image", "publish_artifact"], ...] = ()
    mcp: tuple[MCPBinding, ...] = ()
    functions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.local)) != len(self.local) or len(set(self.functions)) != len(self.functions):
            raise ValueError("Tool bindings must be unique")
        if len({binding.server_id for binding in self.mcp}) != len(self.mcp):
            raise ValueError("Each MCP server has one binding")
        return self


class SkillBinding(Configuration):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    path: Path | None = None
    name: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=640)
    required: bool = False
    revision: int | None = Field(default=None, ge=1)


class SummaryPolicy(Configuration):
    # The generated allowance includes reasoning; memory_tokens only bounds prose.
    max_output_tokens: int = Field(default=8192, ge=256, le=131072)
    max_attempts: int = Field(default=3, ge=1, le=3)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    thinking: Literal["enabled", "disabled"] | None = None


class ContextPolicy(Configuration):
    input_tokens: int = Field(default=32768, ge=512, le=1048576)
    pressure: float = Field(default=0.75, gt=0, lt=1)
    tool_inline_tokens: int = Field(default=2048, ge=64)
    memory_tokens: int = Field(default=2048, ge=64)
    safety_tokens: int = Field(default=1024, ge=128)
    recent_turns: int = Field(default=8, ge=1, le=100)
    summary: SummaryPolicy = Field(default_factory=SummaryPolicy)


class ProjectWorkspace(Configuration):
    """Execution defaults shared by a project's Agents unless overridden."""
    work: Path | None = None
    references: tuple[Path, ...] = ()
    writable_directories: tuple[Path, ...] = ()
    writable_project_paths: tuple[str, ...] = ()
    shell: tuple[str, ...] = ()
    environment: dict[str, str] = Field(default_factory=dict)
    network: Literal["deny", "allow"] = "deny"


class Workspace(ProjectWorkspace):
    inherit_project: bool = True
    project: Path | None = None
    command_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    sandbox: Literal["native", "trusted"] = "native"
    network: Literal["deny", "allow"] | None = None
    command_output_bytes: int = Field(default=16777216, ge=1024, le=1073741824)
    command_inline_bytes: int = Field(default=8192, ge=256, le=65536)

    def resolved(self, defaults=None, folder=None):
        """Resolve project defaults once; the run keeps the effective snapshot."""
        defaults = defaults if self.inherit_project and defaults is not None else ProjectWorkspace()
        inherited = ("work", "references", "writable_directories", "writable_project_paths", "shell")
        return self.model_copy(update={
            "inherit_project": False,
            "project": self.project or (Path(folder) if folder else None),
            **{field: getattr(self, field) or getattr(defaults, field) for field in inherited},
            "environment": {**defaults.environment, **self.environment},
            "network": self.network or defaults.network or "deny",
        })


class Budget(Configuration):
    max_model_calls: int | None = Field(default=None, ge=1)
    max_total_input_tokens: int | None = Field(default=None, ge=1)
    max_uncached_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int = Field(default=262144, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_elapsed_seconds: int = Field(default=86400, ge=1)
    max_stalled_model_calls: int | None = Field(default=None, ge=1)
    max_cost: Decimal | None = Field(default=None, gt=0, description="Local dispatch ceiling using input estimates and output caps; provider reports can correct the original reservation.")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    finalization_fraction: float = Field(default=0.1, ge=0, le=0.5)
    recovery_fraction: float = Field(default=0.05, ge=0, le=0.5)

    @model_validator(mode="after")
    def exploration_room(self):
        if self.finalization_fraction + self.recovery_fraction >= 1:
            raise ValueError("Keep some budget available for exploration")
        return self


class Delivery(Configuration):
    text_required: bool = True
    min_artifacts: int = Field(default=0, ge=0)
    output_schema: dict | None = None

    @field_validator("output_schema")
    @classmethod
    def valid_schema(cls, value):
        if value is not None:
            from oea.contracts.schema_validation import check_schema
            from oea.contracts.errors import RuntimeFault
            from jsonschema.exceptions import SchemaError
            try:
                check_schema(value)
            except (SchemaError, RuntimeFault):
                raise ValueError("Delivery output_schema must be valid JSON Schema") from None
        return value


class Execution(Configuration):
    graph: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    graph_version: str = Field(default="1", min_length=1, max_length=64)
    program: tuple[str, ...] = ()
    program_uses_model: bool = False
    program_timeout_seconds: float = Field(default=3600, gt=0, le=3600)
    max_steps: int | None = Field(default=None, ge=1, le=100000)
    budget: Budget = Field(default_factory=Budget)
    delivery: Delivery = Field(default_factory=Delivery)

    @model_validator(mode="after")
    def driver(self):
        if self.program and self.graph != "default":
            raise ValueError("Choose one program or a registered code graph")
        return self


class CapabilityUse(Configuration):
    allowed: tuple[str, ...] | None = None
    required: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if self.allowed is not None and (len(set(self.allowed)) != len(self.allowed) or not set(self.required) <= set(self.allowed)):
            raise ValueError("Required capabilities must be allowed and names must be unique")
        if len(set(self.required)) != len(self.required):
            raise ValueError("Required capabilities must be unique")
        return self


class UsePolicy(Configuration):
    tools: CapabilityUse = Field(default_factory=CapabilityUse)
    skills: CapabilityUse = Field(default_factory=CapabilityUse)


class Communication(Configuration):
    departments: tuple[str, ...] = ()
    can_request: bool = False


class Manager(Configuration):
    model: Model = Field(default_factory=Model)
    summary_model: Model | None = None
    instructions: Instructions = Field(default_factory=Instructions)
    communication: Communication = Field(default_factory=Communication)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    tools: Tools | None = None
    skills: tuple[SkillBinding, ...] | None = None
    workspace: Workspace | None = None
    execution: Execution | None = None


class AgentDefinition(Configuration):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    project_id: str | None = None
    enabled: bool = True
    removed: bool = Field(default=False, json_schema_extra={"readOnly": True})
    description: str = Field(default="", max_length=640)
    model: Model = Field(default_factory=Model)
    summary_model: Model | None = None
    instructions: Instructions = Field(default_factory=Instructions)
    tools: Tools = Field(default_factory=Tools)
    skills: tuple[SkillBinding, ...] = ()
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    workspace: Workspace = Field(default_factory=Workspace)
    execution: Execution = Field(default_factory=Execution)
    communication: Communication = Field(default_factory=Communication)
    manager: Manager | None = None

    @model_validator(mode="after")
    def unique_skills(self):
        if len({skill.id for skill in self.skills}) != len(self.skills):
            raise ValueError("Skill IDs must be unique")
        return self


def manager_snapshot(definition):
    """An optional role override uses the same capability types as any Agent."""
    manager = definition.manager
    if manager is None:
        raise ValueError("Manager is not configured")
    return definition.model_copy(update={"model": manager.model, "summary_model": manager.summary_model, "instructions": manager.instructions,
        "communication": manager.communication, "context": manager.context,
        **{field: getattr(manager, field) if getattr(manager, field) is not None else getattr(definition, field)
           for field in ("tools", "skills", "workspace", "execution")}})
