"""OAP identities and explicit application capability grants."""

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from oea.contracts.configuration import Budget, Configuration, Tools, UsePolicy, Workspace
from oea.contracts.domain import RunDisplay, RunSpec
from oea.contracts.execution import ResumeExecutionLimits

OAP_VERSION = "1.0"
OAP_PREFIX = "/api/oap/v1"


class ExecutionScope(Configuration):
    """Persistent ownership, resolved by the host rather than supplied by callers."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["platform", "application"] = "platform"
    app_id: str | None = None
    binding_id: str | None = None
    conversation_id: str | None = None

    @model_validator(mode="after")
    def complete_identity(self):
        identities = (self.app_id, self.binding_id, self.conversation_id)
        if self.source == "application" and not all(identities):
            raise ValueError("Application execution requires its complete ownership chain")
        if self.source == "platform" and any(value is not None for value in identities):
            raise ValueError("Platform execution cannot carry application ownership")
        return self


class ApplicationGrant(Configuration):
    """An administrator's maximum grant; source capabilities still apply."""

    model: bool = True
    tools: Tools = Field(default_factory=Tools)
    skills: tuple[str, ...] = ()
    agent_md: bool = False
    workspace: Workspace = Field(default_factory=lambda: Workspace(inherit_project=False))
    budget: Budget = Field(default_factory=Budget)
    trusted_extensions: bool = False

    @model_validator(mode="after")
    def explicit_workspace(self):
        if self.workspace.project or self.workspace.work or self.workspace.writable_project_paths:
            raise ValueError("Application workspaces are host-owned; use explicit reference or writable directories")
        if self.workspace.inherit_project:
            raise ValueError("Application grants cannot inherit project environments")
        if self.workspace.sandbox != "native":
            raise ValueError("Application workloads require the native sandbox")
        if self.tools.functions and not self.trusted_extensions:
            raise ValueError("In-process function tools require explicit trusted extension authorization")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("Grant each Skill once")
        return self


class ApplicationBinding(Configuration):
    app_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    grant: ApplicationGrant = Field(default_factory=ApplicationGrant)
    enabled: bool = True


class ConversationCreate(Configuration):
    binding_id: str = Field(min_length=1, max_length=128)
    name: str = Field(default="Conversation", min_length=1, max_length=80)


class ApplicationRun(Configuration):
    input: str | dict
    policy: UsePolicy = Field(default_factory=UsePolicy)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=32)
    display: RunDisplay = Field(default_factory=RunDisplay)

    @model_validator(mode="after")
    def bounded_work(self):
        RunSpec(instance_id="validation", idempotency_key="validation", **self.model_dump())
        return self


class ApplicationResume(Configuration):
    input: str | dict | None = None
    execution_limits: ResumeExecutionLimits | None = None


class ApplicationResolution(Configuration):
    evidence: str = Field(min_length=1, max_length=8192)
    outcome: Literal["completed", "not_applied"]
    result: dict | None = None


class ApplicationUpload(Configuration):
    conversation_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=240)
    bytes: int = Field(ge=0, le=50 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
