"""Durable thread directory bindings and detached Git worktree preparation."""

import asyncio
from pathlib import Path
import subprocess

from .errors import RuntimeFault, require
from .models import content_hash, now_ms
from .paths import safe_child
from .store import emit, one


def git(repository, *args):
    try:
        result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeFault("WORKDIR_UNAVAILABLE", "Git is unavailable or its operation did not finish within 60 seconds") from exc
    require(result.returncode == 0, "WORKDIR_UNAVAILABLE", "Git could not complete the requested worktree operation",
            details={"operation": args[0], "exit_code": result.returncode})
    return result.stdout.strip()


class Workdirs:
    def __init__(self, runtime):
        self.rt = runtime
        self.pending = {}

    async def get(self, thread_id):
        def read(db):
            from .services import need
            need(db, "threads", thread_id)
            found = one(db, "SELECT * FROM thread_workdirs WHERE thread_id=?", (thread_id,))
            return self.view(found) if found else None
        return await self.rt.store.call(read)

    @staticmethod
    def view(row):
        return {name: row[name] for name in ("thread_id", "kind", "repository", "commit_sha", "path", "state")}

    async def prepare(self, thread_id, idempotency_key, kind="managed", commit=None):
        self.rt.admit()
        require(kind in {"managed", "git"} and isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256,
                "INVALID_REQUEST", "Choose managed or git and a stable preparation key")
        require((kind == "managed" and commit is None) or (kind == "git" and isinstance(commit, str) and 0 < len(commit) <= 256
                and not commit.startswith("-") and "\x00" not in commit), "INVALID_REQUEST", "A Git worktree needs an explicit commit or ref")
        digest = content_hash({"kind": kind, "commit": commit})
        pending = self.pending.get(thread_id)
        if pending:
            require(pending[0] == (idempotency_key, digest), "RESOURCE_BUSY", "Another directory preparation owns this thread")
            return await asyncio.shield(pending[1])
        job = asyncio.create_task(self._prepare(thread_id, idempotency_key, digest, kind, commit))
        self.pending[thread_id] = ((idempotency_key, digest), job)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            # The administrative filesystem intent keeps its owner through its
            # bounded operation; a dropped transport never creates a second writer.
            await job
            raise
        finally:
            self.pending.pop(thread_id, None)

    async def _prepare(self, thread_id, key, digest, kind, commit):
        def inspect(db):
            from .services import need, resolve_project
            thread = need(db, "threads", thread_id)
            existing = one(db, "SELECT * FROM thread_workdirs WHERE thread_id=?", (thread_id,))
            if existing:
                require(existing.idempotency_key == key and existing.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Thread directory already has a different binding")
                return existing, None
            session = need(db, "sessions", thread.session_id)
            project = resolve_project(db, session.project_id)
            require(session.state == "OPEN", "SESSION_CLOSED", "Session is not open")
            require(not thread.active_run_id and not thread.owner_task_id and not thread.blocked_reason
                    and not one(db, "SELECT 1 FROM tasks WHERE thread_id=? AND state='QUEUED' LIMIT 1", (thread_id,)),
                    "THREAD_BUSY", "Prepare a directory only while the thread is idle")
            return None, project.local_folder
        existing, repository = await self.rt.store.call(inspect)
        if existing and existing.state == "READY":
            return {**self.view(existing), "reused": True}
        if existing:
            binding = existing
        else:
            require(kind != "git" or repository, "WORKDIR_UNAVAILABLE", "The project needs a local Git repository")
            sha = await asyncio.to_thread(git, repository, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}") if kind == "git" else None
            path = safe_child(self.rt.root / "workdirs", thread_id)
            def reserve(db):
                from .services import need
                thread = need(db, "threads", thread_id)
                session = need(db, "sessions", thread.session_id)
                require(session.state == "OPEN" and need(db, "projects", session.project_id).local_folder == repository,
                        "REVISION_CONFLICT", "Project or session changed during directory preparation")
                require(not thread.active_run_id and not thread.owner_task_id and not thread.blocked_reason
                        and not one(db, "SELECT 1 FROM tasks WHERE thread_id=? AND state='QUEUED' LIMIT 1", (thread_id,)),
                        "THREAD_BUSY", "Thread started work during preparation")
                db.execute("INSERT INTO thread_workdirs VALUES(?,?,?,?,?,'PREPARING',?,?,?,?)",
                           (thread_id, kind, repository if kind == "git" else None, sha, str(path), key, digest, now_ms(), now_ms()))
                return one(db, "SELECT * FROM thread_workdirs WHERE thread_id=?", (thread_id,))
            binding = await self.rt.store.call(reserve, transaction=True)
        def materialize():
            path = safe_child(self.rt.root / "workdirs", thread_id)
            require(str(path) == binding.path, "WORKDIR_UNAVAILABLE", "Stored directory no longer matches this host")
            path.parent.mkdir(parents=True, exist_ok=True)
            if binding.kind == "managed":
                if path.exists():
                    require(path.is_dir() and not any(path.iterdir()), "WORKDIR_UNCERTAIN", "Existing preparation has files; inspect it before changing the binding")
                else:
                    path.mkdir()
                return
            if not path.exists():
                git(binding.repository, "worktree", "add", "--detach", str(path), binding.commit_sha)
            require(Path(git(path, "rev-parse", "--show-toplevel")).resolve() == path
                    and git(path, "rev-parse", "HEAD") == binding.commit_sha,
                    "WORKDIR_UNCERTAIN", "Existing worktree does not match the frozen source")
            shared = lambda location: Path(git(location, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            require(shared(path) == shared(binding.repository) and not git(path, "status", "--porcelain", "--untracked-files=all"),
                    "WORKDIR_UNCERTAIN", "Worktree preparation is incomplete or its files changed; no files were overwritten")
        try:
            await asyncio.to_thread(materialize)
        except Exception:
            await self.rt.store.call(lambda db: db.execute("UPDATE thread_workdirs SET state='UNCERTAIN',updated_at=? WHERE thread_id=?",
                                                          (now_ms(), thread_id)), transaction=True)
            raise
        def finish(db):
            from .services import need
            db.execute("UPDATE thread_workdirs SET state='READY',updated_at=? WHERE thread_id=?", (now_ms(), thread_id))
            emit(db, "thread.workdir_prepared", session_id=need(db, "threads", thread_id).session_id,
                 thread_id=thread_id, payload={"kind": binding.kind, "commit": binding.commit_sha})
        await self.rt.store.call(finish, transaction=True)
        self.rt.notify()
        return {**self.view(binding), "state": "READY", "reused": bool(existing)}

    def snapshot(self, db, thread_id):
        binding = one(db, "SELECT * FROM thread_workdirs WHERE thread_id=?", (thread_id,))
        require(not binding or binding.state == "READY", "WORKDIR_UNAVAILABLE", "Thread directory preparation is not confirmed")
        return self.view(binding) if binding else None
