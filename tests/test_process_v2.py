"""Actual process Agent lifecycle with UTF-8 JSONL and host capabilities."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Completion, Execution, Model, RunSpec, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import rows
from oea.stdio.process_tree import OwnedProcessTree


PROGRAM = '''
import asyncio
from oea.stdio import AgentDescriptor, ProgramResult, run_agent
class Program:
    def describe(self):
        return AgentDescriptor(agent_id="program", supports_suspend=True, supports_cancel=True, checkpoint_format="counter-v1")
    async def execute(self, ctx):
        mode = ctx.input["mode"]
        if mode == "failure":
            raise ValueError("fixture failure")
        if mode == "cancel":
            await ctx.invoke_tool("run_command", {"argv": [ctx.input["python"], "-B", "-c", "import time; time.sleep(60)"]}, "sleep")
            return ProgramResult(text="Unexpected completion")
        if mode == "model":
            response = await ctx.complete("one")
            return ProgramResult(text=response["message"]["content"])
        script = "from pathlib import Path; p=Path('counter.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('完成')"
        arguments = {"argv": [ctx.input["python"], "-B", "-c", script]}
        first = await ctx.invoke_tool("run_command", arguments, "compute-once")
        second = await ctx.invoke_tool("run_command", arguments, "compute-once")
        assert first == second
        await ctx.progress("计算完成，原回执已保留")
        if mode == "suspend" and not ctx.checkpoint:
            return ProgramResult(status="suspended", checkpoint={"phase": 1}, wait={"kind": "input", "prompt": "确认结果"})
        artifact = await ctx.invoke_tool("publish_artifact", {"path": "counter.txt", "name": "counter.txt"}, "publish-once")
        text = await ctx.invoke_tool("read_file", {"path": "counter.txt"}, "read-once")
        return ProgramResult(text="结果为 1", data={"count": 1, "reply": ctx.reply, "checkpoint": ctx.checkpoint})
run_agent(Program())
'''


class ModelFixture:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        return Completion(text="Model result", usage={"prompt_tokens": 20, "completion_tokens": 3})


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-process-")
        self.root = Path(self.temporary.name)
        self.script = self.root / "进程 agent.py"
        self.script.write_text(PROGRAM, encoding="utf-8")
        self.model = ModelFixture()
        self.host = await Host(self.root / "state", allow_commands=True, model_api=self.model).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.session = await self.host.sessions.create("p")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def configure(self, *, model=False, script=None, timeout=20):
        value = AgentDefinition(id="program", name="Program", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="protocol-fixture") if model else Model(),
            tools=Tools(local=("run_command", "read_file", "publish_artifact")),
            workspace=Workspace(environment={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}),
            execution=Execution(program=(sys.executable, "-B", str(script or self.script)), program_uses_model=model, program_timeout_seconds=timeout))
        await self.host.agents.save(value)

    async def submit(self, mode):
        instance = await self.host.instances.create(self.session["id"], "program")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input={"mode": mode, "python": sys.executable}, idempotency_key="work"))
        return instance, run

    async def test_actual_command_artifact_and_original_tool_receipt_replay(self):
        await self.configure()
        instance, run = await self.submit("normal")
        result = await self.host.runs.result(run.id, timeout=15)
        self.assertEqual(result.text, "结果为 1")
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual((await self.host.artifacts.read(result.artifacts[0].id, "p"))["text"], "1")
        actions = await self.host.store.call(lambda db: rows(db, "SELECT name,state FROM actions WHERE run_id=?", (run.id,)))
        self.assertEqual(sum(value["name"] == "run_command" for value in actions), 1)
        stopped = await self.host.store.call(lambda db: rows(db, "SELECT data_json FROM events WHERE run_id=? AND kind='program.stopped'", (run.id,)))
        self.assertEqual(json.loads(stopped[0]["data_json"])["exit_code"], 0)

    async def test_suspend_restart_reply_uses_checkpoint_without_repeating_command(self):
        await self.configure()
        instance, run = await self.submit("suspend")
        async with asyncio.timeout(15):
            while (await self.host.runs.get(run.id))["state"] not in {"waiting", "failed"}:
                await self.host.run_signal(run.id).wait()
        self.assertEqual((await self.host.runs.get(run.id))["state"], "waiting")
        await self.host.aclose()
        self.host = await Host(self.root / "state", allow_commands=True, model_api=self.model).__aenter__()
        await self.host.runs.resume(run.id, "answer", input="确认")
        result = await self.host.runs.result(run.id, timeout=15)
        self.assertEqual(result.data["checkpoint"], {"phase": 1})
        self.assertEqual(result.data["reply"], "确认")
        self.assertEqual((await self.host.artifacts.read(result.artifacts[0].id, "p"))["text"], "1")
        actions = await self.host.store.call(lambda db: rows(db, "SELECT name FROM actions WHERE run_id=?", (run.id,)))
        self.assertEqual(sum(value["name"] == "run_command" for value in actions), 1)
        self.assertEqual(sum(value["name"] == "_oea_program" for value in actions), 2)

    async def test_program_model_uses_host_budget_and_never_requires_child_credentials(self):
        await self.configure(model=True)
        _, run = await self.submit("model")
        result = await self.host.runs.result(run.id, timeout=15)
        self.assertEqual(result.text, "Model result")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(result.usage["provider_reported"]["input_tokens"], 20)

    async def test_sdk_failure_is_not_a_completed_boundary_and_can_be_explicitly_repaired(self):
        await self.configure()
        _, run = await self.submit("failure")
        with self.assertRaises(RuntimeFault) as failure:
            await self.host.runs.result(run.id, timeout=15)
        self.assertEqual(failure.exception.code, "PROGRAM_FAILED")
        self.assertFalse((await self.host.runs.get(run.id))["result_available"])
        self.script.write_text(PROGRAM.replace('if mode == "failure":', 'if False:'), encoding="utf-8")
        await self.host.runs.resume(run.id, "repair")
        self.assertEqual((await self.host.runs.result(run.id, timeout=15)).text, "结果为 1")

    async def test_cancel_cleans_actual_owned_command_and_program_processes(self):
        await self.configure()
        ready, children = asyncio.Event(), []
        def observed(process):
            tree = OwnedProcessTree(process)
            children.append(process)
            ready.set()
            return tree
        with patch("oea.workspace.service.OwnedProcessTree", side_effect=observed):
            _, run = await self.submit("cancel")
            await asyncio.wait_for(ready.wait(), 15)
            result = await self.host.runs.cancel(run.id)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(children)
        self.assertTrue(all(child.returncode is not None for child in children))
        events = await self.host.store.call(lambda db: rows(db, "SELECT data_json FROM events WHERE run_id=? AND kind='program.stopped'", (run.id,)))
        self.assertEqual(len(events), 1)

    async def test_old_protocol_is_rejected_before_program_execution(self):
        old = self.root / "old_protocol.py"
        old.write_text('import json,sys; request=json.loads(sys.stdin.readline()); print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"protocol_version":"1.0","agent_id":"program"}}),flush=True); sys.stdin.readline()', encoding="utf-8")
        await self.configure(script=old)
        _, run = await self.submit("normal")
        with self.assertRaises(RuntimeFault) as failure:
            await self.host.runs.result(run.id, timeout=15)
        self.assertIn("PROTOCOL_VERSION_UNSUPPORTED", json.dumps(failure.exception.as_dict()))
        self.assertEqual(self.model.calls, 0)
