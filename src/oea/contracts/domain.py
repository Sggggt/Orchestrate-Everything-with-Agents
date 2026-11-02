"""Public identities and receipts for the single v3 execution contract."""

from typing import Any, Literal

import json

from pydantic import Field, field_validator, model_validator

from oea.contracts.configuration import Configuration, UsePolicy

RunState = Literal["accepted", "running", "waiting", "completed", "failed", "cancelled", "needs_review"]


class Artifact(Configuration):
    id: str
    name: str
    sha256: str
    bytes: int
    media_type: str
    project_id: str
    session_id: str | None = None
    origin: Literal["run", "upload", "import"] = "run"
    run_id: str | None = None
    instance_id: str | None = None
    content_view: dict | None = None

    @model_validator(mode="after")
    def execution_owner(self):
        if self.origin == "run":
            if not self.run_id or not self.instance_id or not self.session_id:
                raise ValueError("Run artifacts require their actual Run, instance and session")
        elif self.run_id is not None or self.instance_id is not None:
            raise ValueError("Uploaded or imported artifacts do not have an execution owner")
        return self


class RunResult(Configuration):
    content_view: dict | None = None
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    finish_reason: str | None = None
    usage: dict = Field(default_factory=dict)


class RunHandle(Configuration):
    id: str
    instance_id: str
    root_run_id: str | None
    root_workflow_run_id: str | None = None
    budget_id: str
    state: RunState
    reused: bool = False


class RunDisplay(Configuration):
    title: str = Field(default="", max_length=120)
    stage: str = Field(default="", max_length=120)
    item_label: str = Field(default="", max_length=120)


class RunSpec(Configuration):
    instance_id: str
    input: str | dict
    idempotency_key: str = Field(min_length=1, max_length=256)
    parent_run_id: str | None = None
    budget_id: str | None = None
    policy: UsePolicy = Field(default_factory=UsePolicy)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=32)
    display: RunDisplay = Field(default_factory=RunDisplay)

    @field_validator("artifact_ids")
    @classmethod
    def unique_artifacts(cls, value):
        if len(set(value)) != len(value) or any(not identity for identity in value):
            raise ValueError("Assign each named artifact once")
        return value

    @field_validator("input")
    @classmethod
    def bounded_input(cls, value):
        encoded = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("Supply large inputs as workspace files or context references")
        return value


class MessageSpec(Configuration):
    board_id: str
    content: str = Field(min_length=1, max_length=65536)
    mentions: tuple[str, ...] = Field(default=(), max_length=256)
    reply_to: str | None = None
    topic_id: str | None = None
    context_refs: tuple[str, ...] = Field(default=(), max_length=128)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=256)


class AgentDescriptor(Configuration):
    agent_id: str
    protocol_version: Literal["3.0"] = "3.0"
    supports_cancel: bool = False
    supports_probe: bool = False
    supports_suspend: bool = False
    checkpoint_format: str | None = None


class Wait(Configuration):
    kind: Literal["input", "runs", "confirmation"]
    run_ids: tuple[str, ...] = ()
    policy: Literal["all_succeeded", "all_settled"] = "all_settled"
    prompt: str = ""


class Completion(Configuration):
    text: str = ""
    tool_calls: tuple[dict, ...] = ()
    usage: dict = Field(default_factory=dict)
    finish_reason: str = "stop"
    measurements: tuple[dict, ...] = ()
    attempt_facts: dict = Field(default_factory=dict)
    content_view: dict | None = None
    continuation: dict = Field(default_factory=dict)

    @field_validator("continuation")
    @classmethod
    def continuation_fields(cls, value):
        if set(value) - {"reasoning_content", "reasoning_details"}:
            raise ValueError("Unknown model continuation field")
        return value
