"""Resolved filesystem grants and one effective local execution environment."""

import os
import stat
import sys
import shutil
import hashlib
import json
import asyncio
import subprocess
from functools import lru_cache
from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path

from oea.contracts.configuration import Workspace
from oea.contracts.errors import RuntimeFault, require
from oea.workspace.paths import safe_child


def worker_error(error, exit_code):
    try:
        detail = json.loads(error)
    except (ValueError, UnicodeDecodeError):
        return RuntimeFault("FILE_WORKER_FAILED", "File workload failed before returning its result",
                            details={"exit_code": exit_code, "stderr": error.decode("utf-8", errors="replace")[:8192]})
    return RuntimeFault(detail["code"], detail["safe_message"], details={"exit_code": exit_code})


@lru_cache(maxsize=16)
def toolchains(search_path, shell):
    """Discover a fixed set of development programs once per configured PATH."""
    # Empty/relative PATH entries must not turn an Agent's cwd into a host runtime grant.
    search_path = os.pathsep.join(value for value in search_path.split(os.pathsep) if value and Path(value).is_absolute())
    programs = {name: shutil.which(name, path=search_path) for name in ("git", "rg", "node", "pwsh")}
    programs["python"] = sys.executable
    if shell:
        programs["shell"] = shutil.which(shell[0], path=search_path)
    roots = []
    home = Path.home().resolve()
    for path in programs.values():
        if not path:
            continue
        directory = Path(path).resolve().parent
        if os.name == "nt" and directory.name.lower() in {"cmd", "bin"} and (directory.parent / "mingw64").is_dir():
            directory = directory.parent  # Git for Windows keeps its DLLs beside cmd/bin.
        if directory != home and directory != Path(directory.anchor):
            roots.append(str(directory))
    return tuple(dict.fromkeys(roots)), programs


@dataclass(frozen=True)
class WorkspacePaths:
    project_dir: Path | None
    work_dir: Path
    output_dir: Path
    reference_dirs: tuple[Path, ...] = ()
    writable_project_dirs: tuple[Path, ...] = ()
    writable_directories: tuple[Path, ...] = ()
    host_state_dir: Path | None = None

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
        external = tuple(path.expanduser().resolve() for path in settings.writable_directories)
        require(all(not path.is_relative_to(state) and not state.is_relative_to(path) for path in (*references, *external)),
                "PERMISSION_DENIED", "Explicit directories cannot expose host state")
        return cls(project, work, output, references, tuple(writable), external, state)

    @classmethod
    def prepare(cls, settings: Workspace, instance_id, run_id, state_dir, *, binding=None):
        paths = cls.for_instance(settings, instance_id, run_id, state_dir, binding=binding)
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        return paths

    def as_dict(self):
        return {"project_dir": str(self.project_dir) if self.project_dir else None, "work_dir": str(self.work_dir),
                "output_dir": str(self.output_dir), "reference_dirs": [str(path) for path in self.reference_dirs],
                "writable_project_dirs": [str(path) for path in self.writable_project_dirs],
                "writable_directories": [str(path) for path in self.writable_directories]}

    def resolve(self, value, *, write=False):
        require(isinstance(value, str) and value and "\x00" not in value, "INVALID_PATH", "Path must be nonempty text")
        raw = Path(value).expanduser()
        raw = raw if raw.is_absolute() else self.work_dir / raw
        target = raw.resolve()
        roots = (self.work_dir, *self.writable_project_dirs, *self.writable_directories) if write else (
            self.work_dir, *self.reference_dirs, *self.writable_directories, *((self.project_dir,) if self.project_dir else ()))
        require(any(target.is_relative_to(root) for root in roots), "PERMISSION_DENIED", "Path is outside the granted directories")
        # Resolve links before comparing roots; reject write-through hard links
        # and reparse points at the I/O boundary, without scanning directories.
        if write:
            lexical_roots = [root for root in roots if raw.is_relative_to(root)]
            require(lexical_roots, "PERMISSION_DENIED", "Write paths must start within a granted directory")
            boundary = max(lexical_roots, key=lambda root: len(root.parts))
            for part in (raw, *raw.parents):
                if part.exists():
                    info = part.lstat()
                    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                            "PERMISSION_DENIED", "Writes through linked paths are not granted")
                    require(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1,
                            "PERMISSION_DENIED", "Writing hard-linked files is not granted")
                if part == boundary:
                    break
        return target

    @contextmanager
    def parent(self, value, *, write=False, create=False):
        """Hold an authorized directory inode while untrusted commands can rename paths."""
        target = self.resolve(value, write=write)
        if os.name == "nt":
            # Native tools run this operation inside PSEC; trusted mode has no OS isolation.
            if create:
                target.parent.mkdir(parents=True, exist_ok=True)
            yield target, None
            return
        roots = (self.work_dir, *self.writable_project_dirs, *self.writable_directories) if write else (
            self.work_dir, *self.reference_dirs, *self.writable_directories, *((self.project_dir,) if self.project_dir else ()))
        root = max((root for root in roots if target.is_relative_to(root)), key=lambda root: len(root.parts))
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in target.relative_to(root).parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield target, descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def open_read(self, value):
        with self.parent(value) as (path, parent):
            if parent is None:
                with path.open("rb") as stream:
                    yield stream
            else:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(descriptor, "rb") as stream:
                    require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "INVALID_PATH", "Choose a regular file")
                    yield stream


def intersect_workspace(paths, before, after):
    """A pinned workspace can retain only grants still present in its configuration."""
    current_project = after.project.expanduser().resolve() if after.project else None
    references = {path.expanduser().resolve() for path in after.references}
    writable = {(current_project / relative).resolve() for relative in after.writable_project_paths} if current_project else set()
    external = {path.expanduser().resolve() for path in after.writable_directories}
    paths = replace(paths, project_dir=paths.project_dir if current_project == paths.project_dir else None,
                    reference_dirs=tuple(path for path in paths.reference_dirs if path in references),
                    writable_project_dirs=tuple(path for path in paths.writable_project_dirs if path in writable),
                    writable_directories=tuple(path for path in paths.writable_directories if path in external))
    settings = before.model_copy(update={
        "sandbox": "native" if "native" in {before.sandbox, after.sandbox} else "trusted",
        "network": "allow" if before.network == after.network == "allow" else "deny",
        "environment": {key: value for key, value in before.environment.items() if after.environment.get(key) == value},
        "shell": before.shell if before.shell == after.shell else (),
        **{field: min(getattr(before, field), getattr(after, field)) for field in
           ("command_timeout_seconds", "command_output_bytes", "command_inline_bytes")}})
    return paths, settings


@dataclass(frozen=True)
class ExecutionEnvironment:
    paths: WorkspacePaths
    settings: Workspace

    def describe(self):
        runtime_dirs, programs = self.programs()
        policy = {**self.paths.as_dict(), "sandbox": self.settings.sandbox,
                  "network": self.settings.network, "shell": self.settings.shell,
                  "environment": self.settings.environment, "runtime_dirs": runtime_dirs}
        return {"sandbox": self.settings.sandbox, "isolation_required": self.settings.sandbox == "native",
                "backend": ("process_container" if os.name == "nt" else "bubblewrap") if self.settings.sandbox == "native" else "trusted",
                "network": (self.settings.network or "deny") if self.settings.sandbox == "native" else "unrestricted",
                "revision": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
                "programs": programs, "runtime_dirs": runtime_dirs,
                **self.paths.as_dict()}

    def programs(self):
        roots, programs = toolchains(self.settings.environment.get("PATH", os.environ.get("PATH", "")), self.settings.shell)
        roots = tuple(dict.fromkeys((*self.runtime_dirs(), *roots)))
        require(self.paths.host_state_dir is None or all(not self.paths.host_state_dir.is_relative_to(Path(root)) for root in roots),
                "PERMISSION_DENIED", "A runtime directory cannot expose the host state directory")
        return roots, programs

    def shell(self):
        if self.settings.shell:
            return self.settings.shell
        if os.name == "nt":
            _, programs = self.programs()
            program = programs["pwsh"] or str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            return (program, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command")
        return ("/bin/sh", "-c")

    @staticmethod
    @lru_cache(maxsize=1)
    def runtime_dirs():
        return tuple(dict.fromkeys(str(Path(path).resolve()) for path in (sys.prefix, sys.base_prefix, Path(__file__).resolve().parents[2])))

    def available(self):
        if self.settings.sandbox == "native":
            from oea.workspace.native import executable
            executable()

    def variables(self, additions=None):
        temporary = self.paths.work_dir / "tmp"
        temporary.mkdir(exist_ok=True)
        # Never inherit model, MCP or controller credentials into a command.
        names = ("PATH", "SystemRoot", "SystemDrive", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
        env = {key: os.environ[key] for key in names if key in os.environ}
        env.update(self.settings.environment)
        env.update(additions or {})
        env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                   HOME=str(self.paths.work_dir), USERPROFILE=str(self.paths.work_dir),
                   LOCALAPPDATA=str(temporary), APPDATA=str(temporary),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                   OEA_PROJECT_DIR=str(self.paths.project_dir or ""), OEA_WORK_DIR=str(self.paths.work_dir),
                   OEA_OUTPUT_DIR=str(self.paths.output_dir))
        return env

    async def spawn(self, argv, cwd, additions=None, *, timeout=None, limit=65536):
        from oea.stdio.process_tree import OwnedProcessTree
        self.available()
        env = self.variables(additions)
        if self.settings.sandbox == "native":
            from oea.workspace.native import spawn
            runtime_dirs, _ = self.programs()
            process = await spawn({"operation": "exec", "argv": list(argv), "cwd": str(cwd),
                "readonly": list(dict.fromkeys([*runtime_dirs, *map(str, self.paths.reference_dirs), *([str(self.paths.project_dir)] if self.paths.project_dir else [])])),
                "readwrite": [str(root) for root in (self.paths.work_dir, *self.paths.writable_project_dirs, *self.paths.writable_directories)],
                "environment": env, "network": self.settings.network or "deny",
                "timeout_ms": max(1, int((timeout or self.settings.command_timeout_seconds) * 1000))}, limit=limit)
            return process, process
        process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=limit,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
        try:
            return process, OwnedProcessTree(process)
        except BaseException:
            process.terminate()
            await process.wait()
            raise

    async def worker(self, request):
        bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('oea.workspace.worker',run_name='__main__')"
        process, _ = await self.spawn([sys.executable, "-I", "-B", "-X", "utf8", "-c", bootstrap, str(Path(__file__).resolve().parents[2])], self.paths.work_dir)
        process.stdin.write(json.dumps({**request, "paths": self.paths.as_dict()}, ensure_ascii=False).encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        return process

    async def patch(self, arguments):
        from oea.contracts.errors import RuntimeFault
        process = await self.worker({"operation": "patch", "arguments": arguments})
        output, error, _ = await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())
        if process.returncode != 0:
            raise worker_error(error, process.returncode)
        return json.loads(output)

    async def observe_files(self, request):
        process = await self.worker(request)
        output, error, _ = await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())
        if process.returncode != 0:
            raise worker_error(error, process.returncode)
        return json.loads(output)

    async def copy_to(self, path, destination, *, max_bytes):
        from oea.contracts.errors import RuntimeFault
        process = await self.worker({"operation": "read", "path": str(path), "max_bytes": max_bytes})
        stderr = asyncio.create_task(process.stderr.read())
        fingerprint, size = hashlib.sha256(), 0
        try:
            with destination.open("xb") as output:
                while chunk := await process.stdout.read(65536):
                    size += len(chunk)
                    require(size <= max_bytes, "FILE_TOO_LARGE", "File exceeds the publication size limit")
                    fingerprint.update(chunk)
                    output.write(chunk)
            await process.wait()
            error = await stderr
            if process.returncode != 0:
                raise worker_error(error, process.returncode)
            return size, fingerprint.hexdigest()
        except BaseException:
            process.close()
            await asyncio.gather(process.wait(), stderr, return_exceptions=True)
            raise
