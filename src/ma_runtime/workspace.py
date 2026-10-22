"""Run-scoped file tools, shared schemas and artifact-backed model images."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from .errors import RuntimeFault, require
from .models import WORKBENCH_TOOLS, json_dump
from .store import one, rows
from .tools import ToolSpec


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TEXT = {"type": "string"}
PATH = {"type": "string", "description": "Mapped path below /workspace, /project or /resources."}
SCHEMAS = {
    "read_file": schema({"path": PATH, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 32768}}, ["path"]),
    "search_files": schema({"pattern": TEXT, "path": PATH, "query": TEXT,
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["pattern"]),
    "write_file": schema({"path": PATH, "content": {"type": "string", "maxLength": 524288},
        "expected_revision": TEXT}, ["path", "content"]),
    "edit_file": schema({"path": PATH, "old_text": TEXT, "new_text": TEXT, "expected_revision": TEXT},
        ["path", "old_text", "new_text", "expected_revision"]),
    "exec_command": schema({"argv": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 128},
        "cwd": PATH, "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 300}}, ["argv"]),
    "read_image": schema({"path": PATH, "detail": {"enum": ["auto", "low", "high"]}}, ["path"]),
    "publish_artifact": schema({"path": PATH, "name": TEXT}, ["path"]),
}
DESCRIPTIONS = {
    "read_file": "Read bounded UTF-8 text and its revision. Offsets count characters.",
    "search_files": "Find files by glob and optional literal text. Results are bounded.",
    "write_file": "Create a file; replacing a file requires its current revision.",
    "edit_file": "Replace exactly one occurrence with a matching revision.",
    "exec_command": "Run a configured program in the task directory. trusted-local has host user access, without system isolation; use relative file arguments.",
    "read_image": "Read a PNG/JPEG/WebP image for model vision through an artifact reference.",
    "publish_artifact": "Deliver an authorized workspace file as a session artifact.",
}


class Workspace:
    def __init__(self, runtime, scope, session_id):
        self.runtime, self.scope, self.session_id = runtime, scope, session_id
        self.directory = Path(scope["directory"])
        self.directory.mkdir(parents=True, exist_ok=True)

    def mounts(self):
        writable = self.scope["permission_profile"] in {"workspace-write", "trusted-local"}
        mounts = [("/workspace", self.directory, writable)]
        if self.scope["project_folder"]:
            project = Path(self.scope["project_folder"]).resolve()
            require(not project.is_relative_to(self.runtime.root) and not self.runtime.root.is_relative_to(project),
                    "PERMISSION_DENIED", "Project mapping cannot expose platform state")
            mounts.append(("/project", Path(self.scope["project_folder"]),
                writable and self.scope["environment"]["project_write"] and not self.scope.get("project_read_only", False)))
        mounts += [(guest, Path(host), False) for guest, host in self.scope["environment"]["resources"].items()]
        return mounts

    def path(self, name, *, write=False, directory=False):
        require(isinstance(name, str) and name and "\\" not in name and ":" not in name and "\x00" not in name,
                "PERMISSION_DENIED", "Use a mapped workspace path")
        mapped = PurePosixPath(name if name.startswith("/") else "/workspace/" + name)
        require(".." not in mapped.parts, "PERMISSION_DENIED", "Parent traversal is not allowed")
        for guest, root, writable in sorted(self.mounts(), key=lambda item: -len(item[0])):
            if str(mapped) != guest and not str(mapped).startswith(guest + "/"):
                continue
            require(not write or writable, "PERMISSION_DENIED", "Directory is read-only")
            target = root.joinpath(*mapped.relative_to(guest).parts)
            # Reject link/reparse components and multi-link files at the actual I/O boundary.
            for part in (*reversed(target.parents), target):
                if part != root and not part.is_relative_to(root) and not root.is_relative_to(part):
                    continue
                try:
                    info = part.lstat()
                except FileNotFoundError:
                    continue
                require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                        "PERMISSION_DENIED", "Linked paths are not authorized")
                require(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1,
                        "PERMISSION_DENIED", "Hard-linked files are not authorized")
            # Components are joined beneath the grant with no parent traversal;
            # every existing link/reparse component was rejected above.
            if directory:
                require(target.is_dir(), "NOT_FOUND", "Working directory does not exist")
            elif target.exists():
                require(target.is_file(), "INVALID_REQUEST", "Path must identify a regular file")
            return target
        raise RuntimeFault("PERMISSION_DENIED", "Path is outside this task's mounts")

    def read(self, name):
        path = self.path(name)
        require(path.exists(), "NOT_FOUND", "File does not exist")
        require(path.stat().st_size <= 2 * 1024 * 1024, "PAYLOAD_TOO_LARGE", "Use bounded text files")
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeFault("INVALID_REQUEST", "File is not UTF-8 text") from None
        return path, text, hashlib.sha256(data).hexdigest()

    def write(self, arguments, edit=False):
        path = self.path(arguments["path"], write=True)
        if path.exists():
            _, text, revision = self.read(arguments["path"])
            require(arguments.get("expected_revision") == revision, "REVISION_CONFLICT", "Read the current file before replacing it")
        else:
            require(not edit and not arguments.get("expected_revision"), "REVISION_CONFLICT", "File does not match the expected revision")
        if edit:
            require(arguments["old_text"] and text.count(arguments["old_text"]) == 1,
                    "REVISION_CONFLICT", "old_text must match exactly once")
            content = text.replace(arguments["old_text"], arguments["new_text"], 1)
        else:
            content = arguments["content"]
        data = content.encode("utf-8")
        require(len(data) <= 2 * 1024 * 1024, "PAYLOAD_TOO_LARGE", "File content exceeds the write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".oea-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": arguments["path"], "revision": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


class NativeTool:
    def __init__(self, runtime, name):
        self.runtime, self.name = runtime, name

    def describe(self):
        return ToolSpec(name=self.name, input_schema=SCHEMAS[self.name],
            side_effects="EXTERNAL" if self.name == "exec_command" else "LOCAL" if self.name in {"write_file", "edit_file", "publish_artifact", "read_image"} else "NONE",
            timeout_seconds=330, supports_probe=True)

    async def invoke(self, request):
        active = self.runtime.active.get(request["caller_run_id"])
        require(active is not None, "STALE_RUN", "Run is no longer active")
        scope = active.request.workbench
        if active.context._workspace is None:
            active.context._workspace = Workspace(self.runtime, scope, request["session_id"])
        workspace = active.context._workspace
        args = request["arguments"]
        try:
            if self.name == "read_file":
                _, text, revision = await asyncio.to_thread(workspace.read, args["path"])
                offset, limit = args.get("offset", 0), args.get("limit", 8192)
                return {"text": text[offset:offset + limit], "revision": revision,
                    "next_offset": offset + limit if offset + limit < len(text) else None, "characters": len(text)}
            if self.name in {"write_file", "edit_file"}:
                return await asyncio.to_thread(workspace.write, args, self.name == "edit_file")
            if self.name == "search_files":
                def search():
                    root = workspace.path(args.get("path", "/workspace"), directory=True)
                    matches, visited, scanned_bytes = [], 0, 0
                    for directory, directories, files in os.walk(root, followlinks=False):
                        visited += len(directories) + len(files)
                        if visited > 10000:
                            return {"matches": matches, "truncated": True}
                        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()
                            and not (Path(directory) / name).is_junction()]
                        for filename in files:
                            path = Path(directory) / filename
                            if not path.relative_to(root).match(args["pattern"]):
                                continue
                            if len(matches) >= args.get("limit", 25) or scanned_bytes > 32 * 1024 * 1024:
                                return {"matches": matches, "truncated": True}
                            mapped = str(PurePosixPath(args.get("path", "/workspace")) / path.relative_to(root).as_posix())
                            checked = workspace.path(mapped)
                            if args.get("query"):
                                scanned_bytes += checked.stat().st_size
                                if args["query"] not in workspace.read(mapped)[1]:
                                    continue
                            matches.append(mapped)
                    return {"matches": matches, "truncated": False}
                require(not args["pattern"].startswith("/") and ".." not in PurePosixPath(args["pattern"]).parts,
                        "PERMISSION_DENIED", "Use a relative glob")
                return await asyncio.to_thread(search)
            if self.name == "exec_command":
                from .execution import execute
                return await execute(workspace, request)
            path = workspace.path(args["path"])
            require(path.is_file(), "NOT_FOUND", "File does not exist")
            if self.name == "read_image":
                try:
                    from PIL import Image, UnidentifiedImageError
                except ImportError:
                    raise RuntimeFault("DEPENDENCY_MISSING", "Install the workbench image extra") from None
                require(path.stat().st_size <= 8 * 1024 * 1024, "PAYLOAD_TOO_LARGE", "Image exceeds 8 MiB")
                try:
                    with Image.open(path) as picture:
                        require(picture.format in {"PNG", "JPEG", "WEBP"} and picture.width * picture.height <= 20_000_000,
                                "INVALID_REQUEST", "Unsupported or oversized image")
                        width, height = picture.size
                        picture.verify()
                except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
                    raise RuntimeFault("INVALID_REQUEST", "Image cannot be decoded safely") from None
                artifact = await self.runtime.artifacts.put_file(request["session_id"], path,
                    caller_run_id=request["caller_run_id"])
                return {"image": {"artifact_id": artifact.id, "detail": args.get("detail", "auto")},
                    "width": width, "height": height}
            artifact = await self.runtime.artifacts.put_file(request["session_id"], path,
                metadata={"display_name": args.get("name", path.name)}, caller_run_id=request["caller_run_id"])
            return {"artifact_id": artifact.id, "name": artifact.name}
        except RuntimeFault as exc:
            if exc.code == "OUTCOME_UNCERTAIN":
                raise
            return {"isError": True, "error": exc.as_dict()}

    async def reconcile(self, operation_ref):
        # Read-only failures have no external side effect. Writes require concrete evidence.
        if self.name in {"read_file", "search_files"}:
            return {"confirmed": True, "succeeded": False, "evidence": "Read-only invocation has no side effect"}
        return {"confirmed": False, "evidence": "Inspect the task directory and execution before resolving the outcome"}


class Workbench:
    def __init__(self, runtime):
        self.rt = runtime

    def register(self):
        for name in WORKBENCH_TOOLS:
            self.rt.tools.register(NativeTool(self.rt, name), allowed_agent_ids=())

    def authorize(self, request, name):
        scope = request.workbench
        current = self.rt.agent_configs.get(request.agent_id)
        grant = current.workbench if current else None
        require(scope and grant and name in scope["tools"] and name in grant.tools
                and scope["environment_id"] == grant.environment_id,
                "PERMISSION_DENIED", "Workbench tool is not granted to this run")
        env = self.rt.environments.entry(grant.environment_id)["config"]
        require(env.enabled and env.project_id == request.project_id, "PERMISSION_DENIED", "Environment authorization was revoked")
        require(all(env.resources.get(key) == value for key, value in scope["environment"]["resources"].items()),
                "PERMISSION_DENIED", "Resource authorization was revoked")
        require(scope["permission_profile"] == "read-only" or grant.permission_profile == scope["permission_profile"],
                "PERMISSION_DENIED", "Workbench write authorization was revoked")
        if name in {"write_file", "edit_file", "exec_command"}:
            require(not scope["environment"]["project_write"] or env.project_write,
                    "PERMISSION_DENIED", "Project write authorization was revoked")
        if name == "exec_command":
            require(scope["permission_profile"] == grant.permission_profile == "trusted-local" and self.rt.allow_trusted_local,
                    "PERMISSION_DENIED", "The host has not granted trusted-local execution")
            require(scope["environment"]["runner"] == env.runner == "local" and scope["environment"]["network"] == env.network == "inherit",
                    "UNSUPPORTED_CAPABILITY", "The local runner cannot provide filesystem or network isolation")
            require(all(env.programs.get(key) == value for key, value in scope["environment"]["programs"].items()),
                    "PERMISSION_DENIED", "Program authorization was revoked")

    def catalog(self, request):
        current = self.rt.agent_configs.get(request.agent_id)
        grant = current.workbench if current else None
        return [{"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name],
                "parameters": SCHEMAS[name]}} for name in (request.workbench or {}).get("tools", [])
                if grant and name in grant.tools and request.tool_policy.permits(name)
                and (name != "exec_command" or (grant.permission_profile == "trusted-local" and self.rt.allow_trusted_local))]

    async def inspect(self, agent_id=None, task_id=None):
        require(bool(agent_id) != bool(task_id), "INVALID_REQUEST", "Choose one Agent or task")
        if task_id:
            saved = await self.rt.store.call(lambda db: one(db, "SELECT snapshot_json FROM task_workbenches WHERE task_id=?", (task_id,)))
            require(saved, "NOT_FOUND", "Task has no workbench snapshot")
            return json.loads(saved.snapshot_json)
        config = self.rt.agent_configs.get(agent_id)
        require(config, "NOT_FOUND", "Agent is not configured")
        return {"agent_id": agent_id, "workbench": config.workbench.model_dump(mode="json") if config.workbench else None}

    async def calls(self, task_id, cursor="", limit=25, tool_call_id=None):
        if tool_call_id:
            entry = await self.rt.store.call(lambda db: one(db,
                "SELECT c.* FROM tool_calls c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? AND c.id=?",
                (task_id, tool_call_id)))
            require(entry, "NOT_FOUND", "Tool call does not belong to this task")
            return {"id": entry.id, "state": entry.state,
                    "request": json.loads(entry.request_json),
                    "operation_ref": json.loads(entry.external_operation_ref_json) if entry.external_operation_ref_json else None,
                    "result": json.loads(entry.result_json) if entry.result_json else None,
                    "error": json.loads(entry.error_json) if entry.error_json else None}
        require(1 <= limit <= 100, "INVALID_REQUEST", "limit must be 1..100")
        entries = await self.rt.store.call(lambda db: rows(db,
            "SELECT c.id,c.run_id,c.tool_name,c.state,c.error_json,c.parent_stage_id,c.created_at,c.updated_at FROM tool_calls c JOIN runs r ON r.id=c.run_id "
            "WHERE r.task_id=? AND c.id>? ORDER BY c.id LIMIT ?", (task_id, cursor, limit + 1)))
        return {"items": [{"id": entry.id, "tool_name": entry.tool_name, "state": entry.state,
                           "run_id": entry.run_id, "parent_stage_id": entry.parent_stage_id,
                           "duration_ms": entry.updated_at - entry.created_at if entry.state not in {"PENDING", "DISPATCHED", "UNCERTAIN"} else None,
                           "error": json.loads(entry.error_json) if entry.error_json else None} for entry in entries[:limit]],
                "next_cursor": entries[limit - 1].id if len(entries) > limit else None}

    async def reconcile(self, tool_call_id, resolution=None):
        return await self.rt.tools.reconcile(tool_call_id, resolution=resolution)


def tool_messages(call_id, value):
    messages = [{"role": "tool", "tool_call_id": call_id, "content": json_dump(value)}]
    if isinstance(value, dict) and value.get("image"):
        messages.append({"role": "user", "content": [{"type": "oea_image", **value["image"]}]})
    return messages


async def model_images(messages, context):
    """Hydrate references only in the outgoing request; no base64 in checkpoints."""
    result = []
    for message in messages:
        if not isinstance(message.get("content"), list):
            result.append(message)
            continue
        content = []
        for block in message["content"]:
            if block.get("type") != "oea_image":
                content.append(block)
                continue
            artifact = await context._runtime.artifacts.get(block["artifact_id"])
            require(artifact.session_id == context.request.session_id,
                    "PERMISSION_DENIED", "Image belongs to another session")
            require(artifact.media_type in {"image/png", "image/jpeg", "image/webp"} and artifact.size <= 8 * 1024 * 1024,
                    "INVALID_REQUEST", "Unsupported model image artifact")
            path = await context._runtime.artifacts.open(artifact.id)
            data = await asyncio.to_thread(path.read_bytes)
            content.append({"type": "image_url", "image_url": {"url": "data:" + artifact.media_type + ";base64," + base64.b64encode(data).decode(),
                "detail": block.get("detail", "auto")}})
        result.append({**message, "content": content})
    return result
