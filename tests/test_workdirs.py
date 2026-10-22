"""Thread-owned directories and real, isolated detached Git worktrees."""

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ma_runtime import AgentConfig, Runtime, RuntimeFault
from ma_runtime.workspace import Workspace
from test_workbench import FileAgent


class WorkdirTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-workdir-")
        self.root = Path(self.temp.name)
        self.repository = self.root / "project"
        self.repository.mkdir()
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        await self.rt.projects.update("default", local_folder=str(self.repository))
        await self.rt.environments.configure({"id": "files", "project_id": "default", "project_write": True})
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="external", project_id="default",
            workbench={"environment_id": "files", "permission_profile": "workspace-write", "tools": ["read_file", "write_file", "edit_file"]}))
        self.rt.adapters["worker"] = FileAgent()
        self.session = await self.rt.sessions.create()
        self.thread = await self.rt.threads.create(self.session.id, "worker")

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def run_calls(self, key, calls):
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": self.thread.id, "agent_id": "worker",
            "payload": {"calls": calls}, "idempotency_key": key})
        result = await self.rt.tasks.result(task.id, timeout=5)
        return task, result.output["results"]

    async def test_managed_directory_survives_turns_and_replay_preserves_files(self):
        results = await asyncio.gather(*(self.rt.threads.prepare_workdir(self.thread.id, "directory") for _ in range(3)))
        self.assertEqual(len({entry["path"] for entry in results}), 1)
        await self.run_calls("write", [["write_file", {"path": "note.txt", "content": "retained"}]])
        task, read = await self.run_calls("read", [["read_file", {"path": "note.txt"}]])
        self.assertEqual(read[0]["text"], "retained")
        self.assertTrue((await self.rt.threads.prepare_workdir(self.thread.id, "directory"))["reused"])
        scope = await self.rt.workbench.inspect(task_id=task.id)
        workspace = Workspace(self.rt, scope, self.session.id)
        with self.assertRaises(RuntimeFault) as denied:
            workspace.path("/project/new.txt", write=True)
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.threads.prepare_workdir(self.thread.id, "new-key")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    @unittest.skipUnless(shutil.which("git"), "Git is needed for real worktree verification")
    async def test_detached_worktrees_freeze_commit_and_preserve_original_checkout(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repository), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init")
        (self.repository / "source.txt").write_text("original", encoding="utf-8")
        git("add", "source.txt")
        git("-c", "user.name=Example", "-c", "user.email=example@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "Initial source")
        commit = git("rev-parse", "HEAD")
        first = await self.rt.threads.prepare_workdir(self.thread.id, "git", kind="git", commit=commit)
        other = await self.rt.threads.create(self.session.id, "worker")
        second = await self.rt.threads.prepare_workdir(other.id, "git", kind="git", commit=commit)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(first["commit_sha"], commit)
        (Path(first["path"]) / "source.txt").write_text("branch edit", encoding="utf-8")
        self.assertEqual((Path(second["path"]) / "source.txt").read_text(), "original")
        self.assertEqual((self.repository / "source.txt").read_text(), "original")
        self.assertEqual(git("status", "--porcelain"), "")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        replay = await self.rt.threads.prepare_workdir(self.thread.id, "git", kind="git", commit=commit)
        self.assertTrue(replay["reused"])
        self.assertEqual((Path(replay["path"]) / "source.txt").read_text(), "branch edit")

    async def test_uncertain_directory_does_not_overwrite_files_or_allow_tasks(self):
        path = self.rt.root / "workdirs" / self.thread.id
        path.mkdir(parents=True)
        (path / "existing.txt").write_text("keep")
        with self.assertRaises(RuntimeFault) as uncertain:
            await self.rt.threads.prepare_workdir(self.thread.id, "prepare")
        self.assertEqual(uncertain.exception.code, "WORKDIR_UNCERTAIN")
        self.assertEqual((await self.rt.threads.workdir(self.thread.id))["state"], "UNCERTAIN")
        with self.assertRaises(RuntimeFault) as blocked:
            await self.run_calls("blocked", [])
        self.assertEqual(blocked.exception.code, "WORKDIR_UNAVAILABLE")
        self.assertEqual((path / "existing.txt").read_text(), "keep")
