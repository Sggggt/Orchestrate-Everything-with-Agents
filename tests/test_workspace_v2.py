"""Files and actual commands share a stable real-path workspace."""

import hashlib
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from oea.contracts.configuration import Workspace
from oea.contracts.errors import RuntimeFault
from oea.workspace.service import LocalTool, WorkspacePaths
from oea.workspace.commands import CommandSessions


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-v2-目录 ")
        self.root = Path(self.temporary.name)
        self.project = self.root / "原始项目"
        self.project.mkdir()
        (self.project / "source.txt").write_text("original evidence", encoding="utf-8")
        self.settings = Workspace(project=self.project, work=self.root / "工作目录", sandbox="trusted")
        self.sessions = CommandSessions(self.root / "commands")
        self.paths = WorkspacePaths.prepare(self.settings, "instance", "run-one", self.root / "state")

    async def asyncTearDown(self):
        await self.sessions.close()
        self.temporary.cleanup()

    async def invoke(self, name, arguments):
        async def resolve(run_id):
            return self.paths, self.settings

        async def publish(run_id, path, name):
            return {"id": name, "path": str(path), "bytes": path.stat().st_size}

        provider = LocalTool(name, resolve_run=resolve, publish=publish, commands_enabled=True, sessions=self.sessions)
        return await provider.invoke({"run_id": "run-one", "action_id": "call_" + str(len(list(self.sessions.root.glob("*")))), "arguments": arguments})

    async def test_file_write_then_actual_python_command_read_the_same_path(self):
        written = await self.invoke("apply_patch", {"operation": "add", "path": "输入 数据.txt", "content": "answer=37"})
        self.assertNotIn("isError", written, written)
        result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-c",
            "from pathlib import Path; print(Path('输入 数据.txt').read_text(encoding='utf-8'))"]})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "answer=37")
        self.assertEqual(Path(written["path"]), self.paths.work_dir / "输入 数据.txt")
        self.assertEqual((self.project / "source.txt").read_text(encoding="utf-8"), "original evidence")

    async def test_disabled_commands_request_host_authorization_before_backend_preparation(self):
        async def resolve(run_id):
            return self.paths, self.settings.model_copy(update={"sandbox": "native"})

        provider = LocalTool("exec_command", resolve_run=resolve, publish=None, sessions=self.sessions)
        marker = self.paths.work_dir / "must-not-exist.txt"
        result = await provider.invoke({"run_id": "run-one", "action_id": "denied-command", "arguments": {
            "argv": [sys.executable, "-B", "-c", "from pathlib import Path; Path('must-not-exist.txt').write_text('unexpected')"]}})
        self.assertTrue(result["isError"])
        self.assertEqual(result["error"]["code"], "PERMISSION_DENIED")
        self.assertEqual(result["error"]["continuation"], "request_user_authorization")
        self.assertEqual(result["error"]["details"]["required_host_option"], "--allow-commands")
        self.assertTrue(result["error"]["details"]["restart_required"])
        self.assertFalse(marker.exists())
        self.assertEqual(self.sessions.live, {})

    async def test_read_only_original_and_revision_conflicts_are_preserved(self):
        denied = await self.invoke("apply_patch", {"operation": "add", "path": str(self.project / "source.txt"), "content": "changed"})
        self.assertEqual(denied["error"]["code"], "PERMISSION_DENIED")
        created = await self.invoke("apply_patch", {"operation": "add", "path": "notes.txt", "content": "first"})
        conflict = await self.invoke("apply_patch", {"operation": "add", "path": "notes.txt", "content": "second"})
        self.assertEqual(conflict["error"]["code"], "PATCH_CONFLICT")
        changed = await self.invoke("apply_patch", {"operation": "update", "path": "notes.txt", "hunks": [{"old_text": "first", "new_text": "second"}], "expected_sha256": created["sha256"]})
        self.assertEqual(changed["sha256"], hashlib.sha256(b"second").hexdigest())

    async def test_next_run_preserves_work_and_uses_a_new_output_directory(self):
        await self.invoke("apply_patch", {"operation": "add", "path": "partial.txt", "content": "retained"})
        next_run = WorkspacePaths.prepare(self.settings, "instance", "run-two", self.root / "state")
        self.assertEqual(next_run.work_dir, self.paths.work_dir)
        self.assertEqual((next_run.work_dir / "partial.txt").read_text(), "retained")
        self.assertNotEqual(next_run.output_dir, self.paths.output_dir)

    async def test_known_failed_command_keeps_its_actual_exit_and_output(self):
        result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-c", "import sys; print('evidence'); sys.exit(3)"]})
        self.assertEqual(result["exit_code"], 3)
        self.assertTrue(result["isError"])
        self.assertEqual(result["stdout"].strip(), "evidence")

    async def test_large_command_output_is_saved_and_linked(self):
        result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-c", "print('evidence'*3000)"]})
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 8192)
        reference = result["output_refs"][0]
        self.assertFalse(reference["truncated"])
        self.assertEqual((self.sessions.directory(reference["command_session_id"]) / "stdout.bin").read_text().strip(), "evidence" * 3000)

    async def test_parent_escape_and_hard_link_write_are_rejected(self):
        with self.assertRaises(RuntimeFault):
            self.paths.resolve("../../outside.txt", write=True)
        source = self.project / "source.txt"
        linked = self.paths.work_dir / "linked.txt"
        try:
            os.link(source, linked)
        except OSError:
            self.skipTest("Filesystem does not support hard links")
        denied = await self.invoke("apply_patch", {"operation": "add", "path": str(linked), "content": "changed"})
        self.assertEqual(denied["error"]["code"], "PERMISSION_DENIED")
        self.assertEqual(source.read_text(), "original evidence")

    async def test_utf8_output_pages_preserve_characters_and_byte_offsets(self):
        self.settings.command_inline_bytes = 257
        expected = "数据边界" * 500 + "\n"
        page = await self.invoke("exec_command", {"argv":[sys.executable,"-B","-c","print('数据边界'*500)"]})
        text = page["stdout"]
        while page["stdout_next_offset"] < page["output_refs"][0]["saved_bytes"]:
            offset = page["stdout_next_offset"]
            page = await self.invoke("write_stdin", {"command_session_id":page["command_session_id"], "stdout_offset":offset})
            self.assertGreater(page["stdout_next_offset"], offset)
            text += page["stdout"]
        self.assertEqual(text.replace("\r\n", "\n"), expected)

    async def test_yield_input_incremental_output_and_closed_session_survive_restart(self):
        result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-u", "-c",
            "print('ready', flush=True); print(input(), flush=True)"], "yield_time_ms": 0})
        self.assertEqual(result["state"], "running")
        identity = result["command_session_id"]
        result = await self.invoke("write_stdin", {"command_session_id": identity, "chars": "hello\n", "yield_time_ms": 1000})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].splitlines(), ["ready", "hello"])
        restarted = CommandSessions(self.sessions.root)
        saved = await restarted.observe("run-one", {"command_session_id": identity})
        self.assertEqual(saved["stdout"], result["stdout"])
        empty = await restarted.observe("run-one", {"command_session_id": identity, "stdout_offset": saved["stdout_next_offset"]})
        self.assertEqual(empty["stdout"], "")
        with self.assertRaises(RuntimeFault):
            await restarted.observe("another-run", {"command_session_id": identity})

    async def test_unknown_identity_is_not_replayed_and_output_is_readable(self):
        result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-u", "-c",
            "import time; print('retained', flush=True); time.sleep(30)"], "yield_time_ms": 200})
        restarted = CommandSessions(self.sessions.root)
        saved = await restarted.observe("run-one", {"command_session_id": result["command_session_id"]})
        self.assertEqual(saved["state"], "unknown")
        self.assertFalse(saved["process_stopped"])
        self.assertIn("retained", saved["stdout"])
        self.assertFalse(restarted.live)
        stopped = await self.invoke("write_stdin", {"command_session_id": result["command_session_id"], "terminate": True})
        self.assertTrue(stopped["process_stopped"])
        self.assertEqual(stopped["termination"], "cancelled")

    async def test_timeout_and_retention_report_actual_limits(self):
        self.settings.command_output_bytes = 1024
        result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-u", "-c",
            "import time; print('x'*4096, flush=True); time.sleep(30)"], "timeout_seconds": .3, "yield_time_ms": 1000})
        self.assertEqual(result["termination"], "timeout")
        self.assertTrue(result["process_stopped"])
        self.assertTrue(result["output_refs"][0]["truncated"])
        self.assertEqual(result["output_refs"][0]["saved_bytes"], 1024)

    async def test_commands_do_not_inherit_host_credentials(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"OEA_TEST_SECRET": "not-for-child"}):
            result = await self.invoke("exec_command", {"argv": [sys.executable, "-B", "-c",
                "import os; print(os.environ.get('OEA_TEST_SECRET', 'absent'))"]})
        self.assertEqual(result["stdout"].strip(), "absent")

    async def test_missing_sandbox_never_falls_back_to_trusted(self):
        from unittest.mock import patch
        self.settings.sandbox = "native"
        with patch("oea.workspace.native.executable", side_effect=RuntimeFault("SANDBOX_UNAVAILABLE", "Native component missing")):
            result = await self.invoke("exec_command", {"argv": [sys.executable, "-c", "print('unsafe')"]})
        self.assertEqual(result["error"]["code"], "SANDBOX_UNAVAILABLE")
        self.assertFalse(self.sessions.live)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Linux native component integration")
    async def test_real_sandbox_denies_host_files_network_and_child_escape(self):
        import shutil
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not installed")
        from unittest.mock import patch
        native = patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"]))
        native.start()
        self.addCleanup(native.stop)
        self.settings.sandbox = "native"
        outside = self.root / "private.txt"
        outside.write_text("host-private")
        (self.paths.work_dir / "escape-link").symlink_to(outside)
        program = """
import os, pathlib, socket, subprocess, sys
for path in [sys.argv[1], 'escape-link']:
    try: pathlib.Path(path).read_text()
    except OSError: print('read-denied')
    else: raise AssertionError('outside read')
try: pathlib.Path(sys.argv[1]).write_text('changed')
except OSError: print('write-denied')
else:
    # A fresh namespace may create an ephemeral shadow, never the host file.
    pathlib.Path(sys.argv[1]).unlink()
    print('private-shadow')
s=socket.socket(); s.settimeout(.2)
try: s.connect(('1.1.1.1', 443))
except OSError: print('network-denied')
else: raise AssertionError('network escape')
subprocess.run([sys.executable, '-c', 'import pathlib,sys; assert not pathlib.Path(sys.argv[1]).exists()', sys.argv[1]], check=True)
pathlib.Path('permitted.txt').write_text('written')
"""
        result = await self.invoke("exec_command", {"argv": ["/usr/bin/python3", "-B", "-c", program, str(outside)]})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"].count("read-denied"), 2)
        self.assertIn("network-denied", result["stdout"])
        self.assertEqual(outside.read_text(), "host-private")
        self.assertEqual((self.paths.work_dir / "permitted.txt").read_text(), "written")
