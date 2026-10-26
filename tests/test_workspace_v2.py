"""Files and actual commands share a stable real-path workspace."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from oea.contracts.configuration import Workspace
from oea.contracts.errors import RuntimeFault
from oea.workspace.service import LocalTool, WorkspacePaths


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-v2-目录 ")
        self.root = Path(self.temporary.name)
        self.project = self.root / "原始项目"
        self.project.mkdir()
        (self.project / "source.txt").write_text("original evidence", encoding="utf-8")
        self.settings = Workspace(project=self.project, work=self.root / "工作目录")
        self.paths = WorkspacePaths.prepare(self.settings, "instance", "run-one", self.root / "state")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def invoke(self, name, arguments):
        async def resolve(run_id):
            return self.paths, self.settings

        async def publish(run_id, path, name):
            return {"id": name, "path": str(path), "bytes": path.stat().st_size}

        provider = LocalTool(name, resolve_run=resolve, publish=publish, commands_enabled=True)
        return await provider.invoke({"run_id": "run-one", "action_id": "call", "arguments": arguments})

    async def test_file_write_then_actual_python_command_read_the_same_path(self):
        written = await self.invoke("write_file", {"path": "输入 数据.txt", "content": "answer=37"})
        result = await self.invoke("run_command", {"argv": [sys.executable, "-B", "-c",
            "from pathlib import Path; print(Path('输入 数据.txt').read_text(encoding='utf-8'))"]})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "answer=37")
        self.assertEqual(Path(written["path"]), self.paths.work_dir / "输入 数据.txt")
        self.assertEqual((self.project / "source.txt").read_text(encoding="utf-8"), "original evidence")

    async def test_read_only_original_and_revision_conflicts_are_preserved(self):
        denied = await self.invoke("write_file", {"path": str(self.project / "source.txt"), "content": "changed"})
        self.assertEqual(denied["error"]["code"], "PERMISSION_DENIED")
        created = await self.invoke("write_file", {"path": "notes.txt", "content": "first"})
        conflict = await self.invoke("write_file", {"path": "notes.txt", "content": "second"})
        self.assertEqual(conflict["error"]["code"], "REVISION_CONFLICT")
        changed = await self.invoke("edit_file", {"path": "notes.txt", "old_text": "first", "new_text": "second", "expected_sha256": created["sha256"]})
        self.assertEqual(changed["sha256"], hashlib.sha256(b"second").hexdigest())

    async def test_next_run_preserves_work_and_uses_a_new_output_directory(self):
        await self.invoke("write_file", {"path": "partial.txt", "content": "retained"})
        next_run = WorkspacePaths.prepare(self.settings, "instance", "run-two", self.root / "state")
        self.assertEqual(next_run.work_dir, self.paths.work_dir)
        self.assertEqual((next_run.work_dir / "partial.txt").read_text(), "retained")
        self.assertNotEqual(next_run.output_dir, self.paths.output_dir)

    async def test_known_failed_command_keeps_its_actual_exit_and_output(self):
        result = await self.invoke("run_command", {"argv": [sys.executable, "-B", "-c", "import sys; print('evidence'); sys.exit(3)"]})
        self.assertEqual(result["exit_code"], 3)
        self.assertTrue(result["isError"])
        self.assertEqual(result["stdout"].strip(), "evidence")

    async def test_large_command_output_is_saved_and_linked(self):
        result = await self.invoke("run_command", {"argv": [sys.executable, "-B", "-c", "print('evidence'*3000)"]})
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 8192)
        reference = result["output_refs"][0]
        self.assertFalse(reference["truncated"])
        self.assertEqual(Path(reference["artifact"]["path"]).read_text().strip(), "evidence" * 3000)

    async def test_parent_escape_and_hard_link_write_are_rejected(self):
        with self.assertRaises(RuntimeFault):
            self.paths.resolve("../../outside.txt", write=True)
        source = self.project / "source.txt"
        linked = self.paths.work_dir / "linked.txt"
        try:
            os.link(source, linked)
        except OSError:
            self.skipTest("Filesystem does not support hard links")
        denied = await self.invoke("write_file", {"path": str(linked), "content": "changed"})
        self.assertEqual(denied["error"]["code"], "PERMISSION_DENIED")
        self.assertEqual(source.read_text(), "original evidence")
