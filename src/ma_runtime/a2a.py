"""Internal object mapping pinned to the A2A v1.0.1 ProtoJSON specification.

This module implements no A2A network binding, authentication or push transport.
"""

import base64
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import RuntimeFault, require
from .models import MessageSpec, TaskSpec

SPEC_VERSION = "1.0.1"
EXTENSION = "urn:oea:runtime"
SPEC_URL = "https://github.com/a2aproject/A2A/blob/v1.0.1/specification/a2a.proto"


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    raw: str | None = None
    url: str | None = None
    data: Any = None
    metadata: dict = Field(default_factory=dict)
    filename: str | None = None
    mediaType: str | None = None

    @model_validator(mode="after")
    def content(self):
        selected = self.model_fields_set & {"text", "raw", "url", "data"}
        if len(selected) != 1 or any(getattr(self, key) is None for key in selected - {"data"}):
            raise ValueError("A Part must contain exactly one content variant")
        if "raw" in selected:
            base64.b64decode(self.raw, validate=True)
        if "url" in selected:
            url = urlsplit(self.url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("File URLs must be HTTP(S) references without credentials")
        return self


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messageId: str = Field(min_length=1, max_length=192)
    contextId: str | None = None
    taskId: str | None = None
    role: Literal["ROLE_USER", "ROLE_AGENT"]
    parts: tuple[Part, ...] = Field(min_length=1, max_length=64)
    metadata: dict = Field(default_factory=dict)
    extensions: tuple[str, ...] = Field(default=(), max_length=16)
    referenceTaskIds: tuple[str, ...] = Field(default=(), max_length=32)


def artifact(value):
    """Keep a local artifact reference; never advertise an invented download URL."""
    return {"artifactId": value["id"], "name": value["name"], "parts": [{
        "data": {"artifact_ref": value["id"], "sha256": value["sha256"]},
        "mediaType": "application/vnd.oea.artifact+json",
        "metadata": {EXTENSION: {"media_type": value["media_type"], "size": value["size"]}}}],
        "extensions": [EXTENSION]}


def task(value, artifacts=()):
    state = value["state"]
    mapped = {"QUEUED": "SUBMITTED", "RUNNING": "WORKING", "SUCCEEDED": "COMPLETED",
              "FAILED": "FAILED", "CANCELLED": "CANCELED", "SKIPPED": "REJECTED", "UNCERTAIN": "UNSPECIFIED"}
    if state == "WAITING":
        wait = value.get("wait") or {}
        mapped[state] = "INPUT_REQUIRED" if wait.get("kind") in {"input", "message"} else "WORKING"
    require(state in mapped, "UNSUPPORTED_CAPABILITY", "Task state has no A2A mapping")
    stamp = datetime.fromtimestamp(value["updated_at"] / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    extension = {"state": state, "session_id": value["session_id"], "requires_reconciliation": state == "UNCERTAIN"}
    if value.get("error"):
        extension["error"] = value["error"]
    result = value.get("result")
    status = {"state": "TASK_STATE_" + mapped[state], "timestamp": stamp}
    if result:
        status["message"] = {"messageId": "result:" + value["id"], "contextId": value["thread_id"],
            "taskId": value["id"], "role": "ROLE_AGENT", "parts": [{"data": result}], "extensions": [EXTENSION]}
    return {"id": value["id"], "contextId": value["thread_id"], "status": status,
            "artifacts": [artifact(item) for item in artifacts], "metadata": {EXTENSION: extension}}


def agent_card(config, descriptor, interfaces):
    """Map an Agent to a card for caller-supplied, separately implemented bindings."""
    require(isinstance(interfaces, (list, tuple)) and 0 < len(interfaces) <= 8,
            "UNSUPPORTED_CAPABILITY", "AgentCard mapping requires explicitly supplied interface bindings")
    for interface in interfaces:
        require(set(interface) <= {"url", "protocolBinding", "protocolVersion", "tenant"} and
                interface.get("protocolVersion") == "1.0" and interface.get("protocolBinding") and
                urlsplit(interface.get("url", "")).scheme in {"https", "http"} and
                urlsplit(interface["url"]).hostname and not urlsplit(interface["url"]).username,
                "UNSUPPORTED_CAPABILITY", "Interface must declare an explicit A2A 1.0 binding")
    return {"name": config["name"], "description": config.get("description", ""),
            "supportedInterfaces": list(interfaces), "version": descriptor["adapter_version"],
            "capabilities": {"streaming": False, "pushNotifications": False,
                "extensions": [{"uri": EXTENSION, "required": False, "params": {
                    "agent_id": config["id"], "execution_mode": descriptor["execution_mode"],
                    "supports_suspend": descriptor["supports_suspend"], "cancellation": descriptor["cancellation"],
                    "mapping_spec": SPEC_VERSION}}]},
            "defaultInputModes": ["application/json", "text/plain"],
            "defaultOutputModes": ["application/json", "text/plain"],
            "skills": [{"id": skill["id"], "name": skill["name"], "description": skill.get("description", ""), "tags": []}
                       for skill in config.get("skills", ()) if skill.get("enabled", True)]}


class A2AMapping:
    def __init__(self, runtime):
        self.rt = runtime

    async def capabilities(self):
        return {"spec_version": SPEC_VERSION, "specification": SPEC_URL, "scope": "internal_mapping",
                "network_binding": False, "objects": ["Message", "Task", "Part", "Artifact", "AgentCard"],
                "send_modes": ["message", "task"], "extension": EXTENSION}

    async def send(self, session_id, message, *, mode="message", version=SPEC_VERSION):
        require(version == SPEC_VERSION, "UNSUPPORTED_CAPABILITY", "Unsupported A2A mapping version")
        require(mode in {"message", "task"}, "INVALID_REQUEST", "Choose message or task intent explicitly")
        try:
            value = Message.model_validate(self.rt.clean_json(message))
        except ValueError:
            raise RuntimeFault("A2A_MESSAGE_INVALID", "Message does not match the supported A2A 1.0.1 fields") from None
        require(value.role == "ROLE_USER", "PERMISSION_DENIED", "Incoming messages must have client-to-server role")
        require(set(value.extensions) <= {EXTENSION}, "UNSUPPORTED_CAPABILITY", "Message requests an unsupported extension")
        require(value.contextId, "CONTEXT_REQUIRED", "Choose an existing OEA Thread as contextId")
        thread = await self.rt.threads.get(value.contextId)
        require(thread.session_id == session_id, "PERMISSION_DENIED", "Context belongs to another session")
        if value.taskId:
            associated = await self.rt.tasks.get(value.taskId)
            require(associated.thread_id == thread.id, "PERMISSION_DENIED", "Task and context do not match")
            require(mode == "message", "NEW_TASK_REQUIRED", "New execution intent creates a new task; reference the prior task through referenceTaskIds")
        for identity in value.referenceTaskIds:
            referenced = await self.rt.tasks.get(identity)
            require(referenced.session_id == session_id, "PERMISSION_DENIED", "Referenced task belongs to another session")
        payload = {"a2a": value.model_dump(mode="json", exclude_unset=True)}
        key = "a2a:" + value.messageId
        if mode == "task":
            handle = await self.rt.tasks.submit(TaskSpec(session_id=session_id, thread_id=thread.id,
                agent_id=thread.agent_id, payload=payload, idempotency_key=key))
            return {"task": await self.get_task(handle.id)}
        handle = await self.rt.messages.send(MessageSpec(session_id=session_id, target_thread_id=thread.id,
            message_type="a2a.message", payload=payload, idempotency_key=key))
        return {"message": {"messageId": "receipt:" + handle.id, "contextId": thread.id, "role": "ROLE_AGENT",
            "parts": [{"data": {"message_id": handle.id, "delivery": (await self.rt.messages.delivery(handle.id))["state"]}}],
            "extensions": [EXTENSION]}}

    async def get_task(self, task_id):
        snapshot = await self.rt.tasks.get(task_id)
        artifacts = [await self.rt.artifacts.get(identity) for identity in (snapshot.result.artifact_ids if snapshot.result else ())]
        return task(snapshot.model_dump(mode="json"), [item.model_dump(mode="json") for item in artifacts])
