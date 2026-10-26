"""One real-path resolver for files, commands, and instance output."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from oea.contracts.configuration import Workspace, MODEL_IMAGE_MAX_BYTES, MODEL_IMAGE_MAX_PIXELS
from oea.contracts.measurement import content_view
from oea.contracts.errors import RuntimeFault, require
from oea.stdio.process_tree import OwnedProcessTree
from oea.workspace.paths import safe_child
from oea.security.secrets import atomic_write
from oea.tools.contracts import ToolSpec


def digest(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class WorkspacePaths:
    project_dir: Path | None
    work_dir: Path
    output_dir: Path
    reference_dirs: tuple[Path, ...] = ()
    writable_project_dirs: tuple[Path, ...] = ()

    @classmethod
    def for_instance(cls, settings: Workspace, instance_id, run_id, state_dir, *, binding=None):
        project = settings.project.expanduser().resolve() if settings.project else None
        state = Path(state_dir).resolve()
        if project:
            require(project.is_dir(), "WORKSPACE_UNAVAILABLE", "Project directory is unavailable")
            require(not state.is_relative_to(project) and not project.is_relative_to(state),
                    "PERMISSION_DENIED", "Project cannot expose host state")
        base = settings.work.expanduser().resolve() if settings.work else state / "workspaces"
        if binding and binding.get("work_dir"):
            previous = Path(binding["work_dir"])
            work = safe_child(previous.parent, previous.name)
        else:
            work = (base / instance_id).resolve()
            require(work.is_relative_to(base), "PERMISSION_DENIED", "Instance directory escaped its workspace")
        output = safe_child(work, "outputs/" + run_id)
        writable = []
        for relative in settings.writable_project_paths:
            require(project is not None, "INVALID_REQUEST", "Project write grants need a project")
            path = (project / relative).resolve()
            require(path.is_relative_to(project) and path != project, "PERMISSION_DENIED", "Choose a project subdirectory for writing")
            writable.append(path)
        references = tuple(path.expanduser().resolve() for path in settings.references)
        require(all(not path.is_relative_to(state) and not state.is_relative_to(path) for path in references),
                "PERMISSION_DENIED", "Reference directories cannot expose host state")
        return cls(project, work, output, references, tuple(writable))

    @classmethod
    def prepare(cls, settings: Workspace, instance_id, run_id, state_dir, *, binding=None):
        paths = cls.for_instance(settings, instance_id, run_id, state_dir, binding=binding)
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        return paths

    def as_dict(self):
        return {"project_dir": str(self.project_dir) if self.project_dir else None, "work_dir": str(self.work_dir),
                "output_dir": str(self.output_dir), "reference_dirs": [str(path) for path in self.reference_dirs],
                "writable_project_dirs": [str(path) for path in self.writable_project_dirs]}

    def resolve(self, value, *, write=False):
        require(isinstance(value, str) and value and "\x00" not in value, "INVALID_PATH", "Path must be nonempty text")
        raw = Path(value).expanduser()
        raw = raw if raw.is_absolute() else self.work_dir / raw
        target = raw.resolve()
        roots = (self.work_dir, *self.writable_project_dirs) if write else (
            self.work_dir, *self.reference_dirs, *((self.project_dir,) if self.project_dir else ()))
        require(any(target.is_relative_to(root) for root in roots), "PERMISSION_DENIED", "Path is outside the granted directories")
        # Resolve links before comparing roots; reject write-through hard links
        # and reparse points at the I/O boundary, without scanning directories.
        if write:
            for part in (raw, *raw.parents):
                if part.exists():
                    info = part.lstat()
                    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                            "PERMISSION_DENIED", "Writes through linked paths are not granted")
                    require(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1,
                            "PERMISSION_DENIED", "Writing hard-linked files is not granted")
        return target


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TEXT = {"type": "string"}
PATH = {"type": "string", "description": "Real absolute path, or relative to work_dir."}
SCHEMAS = {
    "read_file": schema({"path": PATH, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 32768}}, ["path"]),
    "search_files": schema({"path": PATH, "pattern": TEXT, "query": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["pattern"]),
    "write_file": schema({"path": PATH, "content": {"type": "string", "maxLength": 1048576}, "expected_sha256": TEXT}, ["path", "content"]),
    "edit_file": schema({"path": PATH, "old_text": TEXT, "new_text": TEXT, "expected_sha256": TEXT}, ["path", "old_text", "new_text", "expected_sha256"]),
    "run_command": schema({"argv": {"type": "array", "items": TEXT, "minItems": 1}, "script": TEXT, "cwd": PATH,
                           "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600}}, []),
    "read_image": schema({"path": PATH, "artifact_id": TEXT}, []) | {"oneOf": [{"required": ["path"]}, {"required": ["artifact_id"]}]},
    "publish_artifact": schema({"path": PATH, "name": TEXT}, ["path"]),
}
DESCRIPTIONS = {
    "read_file": "Read UTF-8 text with a revision hash. Offsets count characters.",
    "search_files": "Find paths by glob and optional literal text within an authorized directory.",
    "write_file": "Create a file. Replacing existing content requires its current sha256.",
    "edit_file": "Replace one unique text occurrence with a matching sha256.",
    "run_command": "Execute argv or a configured Shell script in work_dir. Commands have host user permissions; file grants are not an OS sandbox.",
    "read_image": "Read a granted workspace image path or an explicitly assigned image artifact_id through its durable reference.",
    "publish_artifact": "Publish an authorized file as a run-owned artifact.",
}


class FileWrites:
    """Serialize only a shared file's compare-and-write boundary."""
    def __init__(self):
        self.entries = {}

    async def run(self, path, write):
        key = os.path.normcase(str(path))
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = {"lock": asyncio.Lock(), "users": 0}
        entry["users"] += 1
        async def execute():
            async with entry["lock"]:
                return await asyncio.to_thread(write)
        task = asyncio.create_task(execute())
        def finished(completed):
            # The worker retains ownership even if its consumer leaves.
            entry["users"] -= 1
            if not entry["users"]:
                self.entries.pop(key, None)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)


class LocalTool:
    def __init__(self, name, *, resolve_run, publish, commands_enabled=False, file_writes=None, image_artifact=None):
        self.name, self.resolve_run, self.publish = name, resolve_run, publish
        self.commands_enabled = commands_enabled
        self.image_artifact = image_artifact
        self.file_writes = file_writes if file_writes is not None else FileWrites()
        self.spec = ToolSpec(name=name, description=DESCRIPTIONS[name], input_schema=SCHEMAS[name],
                             side_effects="local" if name in {"write_file", "edit_file", "run_command", "publish_artifact"} else "none",
                             cancellation="owned_process" if name == "run_command" else "none",
                             timeout_seconds=3600 if name == "run_command" else 60)

    def describe(self):
        return self.spec

    async def invoke(self, request):
        paths, settings = await self.resolve_run(request["run_id"])
        args = request["arguments"]
        try:
            if self.name == "run_command":
                require(self.commands_enabled, "PERMISSION_DENIED", "Host has not enabled local commands")
                return await self.command(request, paths, settings)
            if self.name in {"publish_artifact", "read_image"}:
                if self.name == "read_image" and "artifact_id" in args:
                    require(self.image_artifact is not None, "PERMISSION_DENIED", "This provider has no assigned image source")
                    path, artifact = await self.image_artifact(request["run_id"], args["artifact_id"])
                    media, fingerprint = await asyncio.to_thread(self.image_type, path)
                    require(media == artifact["media_type"] and fingerprint == artifact["sha256"], "IMAGE_INVALID", "Assigned image content differs from its saved format or hash")
                    return {"artifact": artifact, "image": True}
                path = paths.resolve(args["path"])
                if self.name == "read_image":
                    require(path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}, "IMAGE_INVALID", "Choose PNG, JPEG, or WebP")
                    media, fingerprint = await asyncio.to_thread(self.image_type, path)
                    artifact = await self.publish(request["run_id"], path, path.name, media_type=media, expected_sha256=fingerprint)
                else:
                    artifact = await self.publish(request["run_id"], path, args.get("name", path.name))
                return {"artifact": artifact, "image": self.name == "read_image"}
            if self.name in {"write_file", "edit_file"}:
                raw = Path(args["path"]).expanduser()
                target = (raw if raw.is_absolute() else paths.work_dir / raw).resolve()
                return await self.file_writes.run(target, lambda: self.files(args, paths))
            return await asyncio.to_thread(self.files, args, paths)
        except RuntimeFault as error:
            return {"isError": True, "error": error.as_dict()}
        except (FileNotFoundError, FileExistsError, IsADirectoryError, NotADirectoryError, UnicodeDecodeError) as error:
            return {"isError": True, "error": {"code": "FILE_UNAVAILABLE", "safe_message": type(error).__name__}}

    @staticmethod
    def image_type(path):
        from PIL import Image, UnidentifiedImageError
        with path.open("rb") as stream:
            raw = stream.read(MODEL_IMAGE_MAX_BYTES + 1)
        require(len(raw) <= MODEL_IMAGE_MAX_BYTES, "IMAGE_TOO_LARGE", "Image input is limited to 5 MiB")
        try:
            with Image.open(BytesIO(raw)) as image:
                require(image.format in {"PNG", "JPEG", "WEBP"} and image.width * image.height <= MODEL_IMAGE_MAX_PIXELS,
                    "IMAGE_INVALID", "Use a supported image with at most 20 million pixels")
                media = Image.MIME[image.format]
                image.verify()
                return media, digest(raw)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise RuntimeFault("IMAGE_INVALID", "Image content is invalid or unsupported") from None

    def files(self, args, paths):
        path = paths.resolve(args.get("path", str(paths.work_dir)), write=self.name in {"write_file", "edit_file"})
        if self.name == "read_file":
            raw = path.read_bytes()
            require(len(raw) <= 16 * 1024 * 1024, "FILE_TOO_LARGE", "Use a program to inspect this file")
            text = raw.decode("utf-8")
            offset, limit = args.get("offset", 0), args.get("limit", 8192)
            return {"path": str(path), "text": text[offset:offset + limit], "sha256": digest(raw),
                    "offset_unit": "unicode_codepoint", "next_offset": offset + limit if offset + limit < len(text) else None,
                    "content_view": content_view("page", source_ref={"path": str(path), "sha256": digest(raw)}, offset=offset,
                        length=len(text[offset:offset + limit]), next_offset=offset + limit if offset + limit < len(text) else None)}
        if self.name == "search_files":
            found, limit = [], args.get("limit", 100)
            require(not Path(args["pattern"]).is_absolute() and ".." not in Path(args["pattern"]).parts,
                    "INVALID_PATH", "Use a relative glob")
            for candidate in path.glob(args["pattern"]):
                if ".git" in candidate.parts or not candidate.is_file():
                    continue
                candidate = paths.resolve(str(candidate))
                if args.get("query") and (candidate.stat().st_size > 1048576 or args["query"] not in candidate.read_text(encoding="utf-8", errors="replace")):
                    continue
                found.append(str(candidate))
                if len(found) >= limit:
                    break
            return {"paths": found, "limit_reached": len(found) == limit}
        previous = path.read_bytes() if path.exists() else None
        expected = args.get("expected_sha256")
        require((previous is None and expected is None) or previous is not None and expected == digest(previous),
                "REVISION_CONFLICT", "Read the current file hash before replacing it")
        if self.name == "edit_file":
            require(previous is not None, "NOT_FOUND", "File does not exist")
            text = previous.decode("utf-8")
            require(args["old_text"] and text.count(args["old_text"]) == 1, "EDIT_CONFLICT", "Old text must match exactly once")
            data = text.replace(args["old_text"], args["new_text"], 1).encode("utf-8")
        else:
            data = args["content"].encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
        return {"path": str(path), "sha256": digest(data), "bytes": len(data)}

    async def command(self, request, paths, settings):
        args = request["arguments"]
        require(bool(args.get("argv")) != bool(args.get("script")), "INVALID_REQUEST", "Choose argv or script")
        if args.get("script"):
            require(settings.shell, "DEPENDENCY_MISSING", "Configure a Shell for scripts")
        argv = args.get("argv") or [*settings.shell, args["script"]]
        cwd = paths.resolve(args.get("cwd", str(paths.work_dir)))
        temporary = paths.work_dir / "tmp"
        temporary.mkdir(exist_ok=True)
        environment = {**os.environ, **settings.environment, "TEMP": str(temporary), "TMP": str(temporary),
                       "TMPDIR": str(temporary), "PYTHONDONTWRITEBYTECODE": "1", "OEA_PROJECT_DIR": str(paths.project_dir or ""),
                       "OEA_WORK_DIR": str(paths.work_dir), "OEA_OUTPUT_DIR": str(paths.output_dir)}
        process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
        try:
            tree = OwnedProcessTree(process)
        except BaseException:
            process.terminate()
            await process.wait()
            raise

        async def drain(stream, label):
            path = paths.resolve(str(paths.output_dir / (request["action_id"] + "." + label + ".txt")), write=True)
            inline, count = bytearray(), 0
            with path.open("wb") as output:
                while chunk := await stream.read(16384):
                    inline.extend(chunk[:max(0, 8192 - len(inline))])
                    output.write(chunk[:max(0, 16 * 1024 * 1024 - count)])
                    count += len(chunk)
            return inline.decode("utf-8", errors="replace"), path, count

        streams = [asyncio.create_task(drain(process.stdout, "stdout")), asyncio.create_task(drain(process.stderr, "stderr"))]
        termination = None
        try:
            await asyncio.wait_for(process.wait(), min(args.get("timeout_seconds", settings.command_timeout_seconds), settings.command_timeout_seconds))
        except (asyncio.CancelledError, TimeoutError) as error:
            await tree.stop()
            termination = "cancelled" if isinstance(error, asyncio.CancelledError) else "timeout"
        except BaseException:
            await tree.stop()
            await asyncio.gather(*streams, return_exceptions=True)
            raise
        finally:
            tree.close()
        output = await asyncio.gather(*streams)
        artifacts = []
        for _, path, size in output:
            if size > 8192:
                artifact = await self.publish(request["run_id"], path, path.name)
                truncated = size > 16 * 1024 * 1024
                artifact["content_view"] = content_view("full", integrity="truncated" if truncated else "complete",
                    source_complete=not truncated, source_ref=artifact["id"], length=artifact["bytes"], offset_unit="byte")
                artifacts.append({"artifact": artifact, "observed_bytes": size, "saved_bytes": artifact["bytes"], "truncated": truncated})
        return {"exit_code": process.returncode, "stdout": output[0][0], "stderr": output[1][0],
                "isError": termination is not None or process.returncode != 0, "output_refs": artifacts,
                "execution": "host user permissions", "cwd": str(cwd), "process_stopped": True, "termination": termination,
                "content_view": content_view("excerpt" if artifacts else "full", integrity="truncated" if any(item["truncated"] for item in artifacts) else "complete",
                    source_complete=not any(item["truncated"] for item in artifacts), source_ref=[item["artifact"]["id"] for item in artifacts])}
