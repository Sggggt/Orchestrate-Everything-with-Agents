"""Several real Agent Runs share files through tools and actual Shell processes."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Completion, Model, RunSpec, Tools, Workspace
from oea.runtime.host import Host
from oea.storage.persistence import rows
from oea.security.secrets import atomic_write


class PlannedModel:
    """Decisions are fixtures; Agents, grants, files, commands and receipts are real."""
    def __init__(self):
        self.plans = {}

    async def complete(self, model, messages, **options):
        if any(message["role"] == "tool" for message in messages):
            return Completion(text="Observed tool receipt", usage={"prompt_tokens": 20, "completion_tokens": 3})
        tool, arguments = self.plans[model.model]
        return Completion(tool_calls=({"id": "operation", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}},),
            finish_reason="tool_calls", usage={"prompt_tokens": 20, "completion_tokens": 3})


class SharedFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-shared-files-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.shared = self.project / "shared"
        self.shared.mkdir(parents=True)
        self.target = self.shared / "counter.txt"
        self.target.write_text("0", encoding="utf-8")
        self.model = PlannedModel()
        self.host = await Host(self.root / "state", model_api=self.model, allow_commands=True).__aenter__()
        await self.host.projects.create("Project", project_id="p", folder=self.project)
        self.session = await self.host.sessions.create("p")
        executable = shutil.which("pwsh") or shutil.which("powershell.exe")
        self.shell = (executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command") if executable else ("/bin/sh", "-c")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def agent(self, identity, tool, arguments):
        self.model.plans[identity] = tool, arguments
        await self.host.agents.save(AgentDefinition(id=identity, name=identity, project_id="p",
            model=Model(base_url="https://model.invalid/v1", model=identity), tools=Tools(local=(tool,)),
            workspace=Workspace(project=self.project, writable_project_paths=("shared",), shell=self.shell)))
        instance = await self.host.instances.create(self.session["id"], identity)
        return await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Apply the shared-file operation", idempotency_key="one-operation"))

    async def receipts(self, name):
        values = await self.host.store.call(lambda db: rows(db, "SELECT id,state,result_json,error_json FROM actions WHERE name=? ORDER BY rowid", (name,)))
        return [{**value, "result": json.loads(value["result_json"]) if value["result_json"] else None} for value in values]

    def script_command(self, arguments):
        if os.name == "nt":
            return "& " + " ".join("'" + str(value).replace("'", "''") + "'" for value in arguments)
        return shlex.join(str(value) for value in arguments)

    async def test_eight_agents_write_and_edit_the_same_revision_have_one_winner(self):
        sha = hashlib.sha256(b"0").hexdigest()
        def delayed_replace(path, data):
            if path == self.target:
                time.sleep(0.05)
            return atomic_write(path, data)
        with patch("oea.workspace.service.atomic_write", delayed_replace):
            runs = []
            for index in range(8):
                identity = "writer" + str(index)
                tool = "write_file" if index % 2 == 0 else "edit_file"
                arguments = {"path": str(self.target), "expected_sha256": sha}
                arguments.update({"content": identity} if tool == "write_file" else {"old_text": "0", "new_text": identity})
                runs.append(await self.agent(identity, tool, arguments))
            await asyncio.gather(*(self.host.runs.result(run.id, timeout=15) for run in runs))
        receipts = await self.receipts("write_file") + await self.receipts("edit_file")
        successful = [value for value in receipts if not value["result"].get("isError")]
        conflicts = [value for value in receipts if value["result"].get("error", {}).get("code") == "REVISION_CONFLICT"]
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(conflicts), 7)
        self.assertTrue(all(value["state"] == "completed" for value in receipts))
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), successful[0]["result"]["sha256"])
        self.assertEqual(self.host.file_writes.entries, {})

    async def test_different_files_are_not_serialized_by_a_global_write_lock(self):
        targets = [self.shared / name for name in ("first.txt", "second.txt")]
        for target in targets:
            target.write_text("0", encoding="utf-8")
        barrier = threading.Barrier(2)
        def parallel_replace(path, data):
            if path in targets:
                barrier.wait(timeout=10)
            return atomic_write(path, data)
        with patch("oea.workspace.service.atomic_write", parallel_replace):
            runs = [await self.agent("writer" + str(index), "write_file", {"path": str(target), "content": "1", "expected_sha256": hashlib.sha256(b"0").hexdigest()}) for index, target in enumerate(targets)]
            await asyncio.gather(*(self.host.runs.result(run.id, timeout=15) for run in runs))
        self.assertTrue(all(not value["result"].get("isError") for value in await self.receipts("write_file")))
        self.assertEqual([target.read_text(encoding="utf-8") for target in targets], ["1", "1"])

    async def test_eight_shell_read_modify_write_commands_lose_updates_without_cooperation(self):
        script = self.root / "increment.py"
        script.write_text("import json,socket,sys\nfrom pathlib import Path\np=Path(sys.argv[1]); before=int(p.read_text())\ns=socket.create_connection(('127.0.0.1',int(sys.argv[2])))\ns.sendall(b'ready\\n'); s.recv(1); s.close()\np.write_text(str(before+1),encoding='utf-8')\nprint(json.dumps({'before':before,'after':before+1}))\n", encoding="utf-8")
        ready, release, connections, handlers = asyncio.Event(), asyncio.Event(), [], set()
        async def connected(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                self.assertEqual(await reader.readline(), b"ready\n")
                connections.append(writer)
                if len(connections) == 8:
                    ready.set()
                await release.wait()
                writer.write(b"G")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)
        server = await asyncio.start_server(connected, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            runs = [await self.agent("shell" + str(index), "run_command", {"script": self.script_command((sys.executable, "-B", script, self.target, port))}) for index in range(8)]
            await asyncio.wait_for(ready.wait(), 30)
            self.assertEqual(len(connections), 8)
            release.set()
            await asyncio.gather(*(self.host.runs.result(run.id, timeout=30) for run in runs))
            receipts = await self.receipts("run_command")
            self.assertEqual(len(receipts), 8)
            self.assertTrue(all(value["result"]["exit_code"] == 0 for value in receipts))
            self.assertTrue(all(json.loads(value["result"]["stdout"].strip())["before"] == 0 for value in receipts))
            self.assertEqual(self.target.read_text(encoding="utf-8"), "1")
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            await asyncio.gather(*tuple(handlers), return_exceptions=True)

    async def test_shell_write_can_bypass_a_file_tool_compare_and_replace_boundary(self):
        blocked, release = threading.Event(), threading.Event()
        def held_replace(path, data):
            if path == self.target:
                blocked.set()
                if not release.wait(timeout=15):
                    raise TimeoutError("Controlled test release was not received")
            return atomic_write(path, data)
        with patch("oea.workspace.service.atomic_write", held_replace):
            try:
                file_run = await self.agent("file-writer", "write_file", {"path": str(self.target), "content": "FILE", "expected_sha256": hashlib.sha256(b"0").hexdigest()})
                self.assertTrue(await asyncio.to_thread(blocked.wait, 5))
                script = self.root / "shell_write.py"
                script.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('SHELL',encoding='utf-8')\n", encoding="utf-8")
                shell_run = await self.agent("shell-writer", "run_command", {"script": self.script_command((sys.executable, "-B", script, self.target))})
                await self.host.runs.result(shell_run.id, timeout=15)
                self.assertEqual(self.target.read_text(encoding="utf-8"), "SHELL")
                release.set()
                await self.host.runs.result(file_run.id, timeout=10)
            finally:
                release.set()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "FILE")
        self.assertFalse((await self.receipts("write_file"))[0]["result"].get("isError"))
        self.assertEqual((await self.receipts("run_command"))[0]["result"]["exit_code"], 0)
