"""The small local tool surface shares authorization and one environment."""

from pathlib import Path
import asyncio

from oea.contracts.errors import RuntimeFault, require
from oea.tools.contracts import ToolSpec
from oea.workspace.environment import WorkspacePaths, ExecutionEnvironment
from oea.workspace.patches import FileWrites, apply_patch
from oea.workspace.media import image_type


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TEXT = {"type": "string"}
PATH = {"type": "string", "description": "Absolute granted path, or relative to work_dir."}
WAIT = {"type": "integer", "minimum": 0, "maximum": 30000}
OFFSET = {"type": "integer", "minimum": 0}
SCHEMAS = {
    "exec_command": schema({"argv": {"type": "array", "items": TEXT, "minItems": 1}, "command": TEXT,
        "cwd": PATH, "env": {"type": "object", "additionalProperties": TEXT}, "yield_time_ms": WAIT,
        "tty": {"type": "boolean", "default": False, "description": "This release uses pipes; true returns CAPABILITY_UNAVAILABLE."},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600}}, [])
        | {"oneOf": [{"required": ["argv"]}, {"required": ["command"]}]},
    "write_stdin": schema({"command_session_id": TEXT, "chars": {"type": "string", "maxLength": 1048576},
        "yield_time_ms": WAIT, "close_stdin": {"type": "boolean"}, "terminate": {"type": "boolean"},
        "stdout_offset": OFFSET, "stderr_offset": OFFSET}, ["command_session_id"]),
    "apply_patch": schema({"path": PATH, "operation": {"enum": ["add", "update", "delete"]},
        "content": {"type": "string", "maxLength": 1048576}, "expected_sha256": TEXT,
        "hunks": {"type": "array", "minItems": 1, "maxItems": 1000, "items": schema({"old_text": TEXT, "new_text": TEXT}, ["old_text", "new_text"])}}, ["path", "operation"])
        | {"allOf": [{"if": {"properties": {"operation": {"const": "add"}}}, "then": {"required": ["content"]}},
                     {"if": {"properties": {"operation": {"const": "update"}}}, "then": {"required": ["hunks"]}},
                     {"if": {"properties": {"operation": {"const": "delete"}}}, "then": {"required": ["expected_sha256"]}}]},
    "view_image": schema({"path": PATH, "artifact_id": TEXT}, []) | {"oneOf": [{"required": ["path"]}, {"required": ["artifact_id"]}]},
    "publish_artifact": schema({"path": PATH, "name": TEXT}, ["path"]),
}
DESCRIPTIONS = {
    "exec_command": "Execute argv or configured Shell text in the effective environment. A short yield returns a running command_session_id; it does not stop the command. Use programs for text reading, searching and batch work.",
    "write_stdin": "Observe saved command output by byte offset, send input, close stdin, or terminate the owned command tree. Never rerun a command to recover its output.",
    "apply_patch": "Add, delete, or update a granted file with unique exact context hunks. Delete requires sha256; a stale optional expected_sha256 returns a conflict.",
    "view_image": "Read a granted image path or assigned image artifact through an immutable model-readable reference.",
    "publish_artifact": "Publish a selected granted file as a run-owned artifact.",
}


class LocalTool:
    def __init__(self, name, *, resolve_run, publish, commands_enabled=False, file_writes=None, image_artifact=None, sessions=None):
        self.name, self.resolve_run, self.publish = name, resolve_run, publish
        self.commands_enabled, self.image_artifact, self.sessions = commands_enabled, image_artifact, sessions
        self.file_writes = file_writes if file_writes is not None else FileWrites()
        self.spec = ToolSpec(name=name, description=DESCRIPTIONS[name], input_schema=SCHEMAS[name],
            side_effects="none" if name == "view_image" else "local",
            cancellation="owned_process" if name == "exec_command" else "none", timeout_seconds=60)

    def describe(self):
        return self.spec

    async def invoke(self, request):
        paths, settings = await self.resolve_run(request["run_id"])
        args = request["arguments"]
        try:
            if self.name == "view_image" and "artifact_id" in args:
                require(self.image_artifact is not None, "PERMISSION_DENIED", "No assigned image source")
                path, artifact = await self.image_artifact(request["run_id"], args["artifact_id"])
                media, fingerprint = await asyncio.to_thread(image_type, path)
                require(media == artifact["media_type"] and fingerprint == artifact["sha256"], "IMAGE_INVALID", "Image differs from its saved revision")
                return {"artifact": artifact, "image": True}
            environment = ExecutionEnvironment(paths, settings)
            environment.available()
            if self.name in {"exec_command", "write_stdin"}:
                require(self.commands_enabled and self.sessions is not None, "PERMISSION_DENIED", "Host has not enabled command sessions")
                if self.name == "exec_command":
                    result = await self.sessions.start(request, environment)
                    require(result["state"] != "unknown", "OUTCOME_UNCERTAIN", "Inspect the original command session before continuing",
                            evidence_ref={"action_id": request["action_id"], "command_session_id": result["command_session_id"]},
                            details={"process_stopped": False, "cause": result.get("error")})
                    return result
                return await self.sessions.observe(request["run_id"], args, environment)
            if self.name == "apply_patch":
                target = paths.resolve(args["path"], write=True)
                if settings.sandbox == "native":
                    result = await self.file_writes.run(target, lambda: environment.patch(args), asynchronous=True)
                else:
                    result = await self.file_writes.run(target, lambda: apply_patch(args, paths))
                return {**result, "environment": environment.describe()}
            path = paths.resolve(args["path"])
            if self.name == "view_image":
                artifact = await self.publish(request["run_id"], path, path.name, validate_image=True)
            else:
                artifact = await self.publish(request["run_id"], path, args.get("name", path.name))
            return {"artifact": artifact, "image": self.name == "view_image"}
        except RuntimeFault as error:
            if error.code == "OUTCOME_UNCERTAIN":
                raise
            return {"isError": True, "error": error.as_dict()}
        except (OSError, UnicodeDecodeError) as error:
            return {"isError": True, "error": {"code": "FILE_UNAVAILABLE", "safe_message": str(error)}}
