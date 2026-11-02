"""Real native command ownership across a persistent wait and abrupt host loss."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Completion, Model, RunSpec, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host


class WaitingCommandModel:
    def __init__(self, command):
        self.command, self.calls = command, 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        return Completion(tool_calls=tuple({"id":name,"type":"function","function":{"name":name,"arguments":json.dumps(arguments)}}
            for name, arguments in (("exec_command", {"argv":[sys.executable,"-B","-u","-c",self.command],"yield_time_ms":200}),
                                    ("wait_for_input", {"prompt":"Inspect the retained command"}))),
            finish_reason="tool_calls", usage={"prompt_tokens":30,"completion_tokens":10})


async def start_waiting(host, *, elapsed=None):
    await host.projects.create("Project", project_id="p")
    session = await host.sessions.create("p")
    definition = AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid", model="fixture"),
        tools=Tools(local=("exec_command","write_stdin")), workspace=Workspace(sandbox="native" if os.environ.get("OEA_TEST_NATIVE_COMPONENT") else "trusted"))
    if elapsed is not None:
        definition.execution.budget.max_elapsed_seconds = elapsed
    await host.agents.save(definition)
    instance = await host.instances.create(session["id"], "worker")
    run = await host.runs.submit(RunSpec(instance_id=instance["id"], input="Keep a command while waiting", idempotency_key="original"))
    async with asyncio.timeout(10):
        while True:
            signal = host.run_signal(run.id)
            value = await host.runs.get(run.id)
            if value["state"] == "waiting":
                return run, definition
            if value["state"] in {"failed","needs_review"}:
                raise AssertionError(value)
            await signal.wait()


class CommandOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_wait_does_not_extend_the_root_command_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            component = os.environ.get("OEA_TEST_NATIVE_COMPONENT")
            if component:
                native = patch("oea.workspace.native.executable", return_value=Path(component))
                native.start()
                self.addCleanup(native.stop)
            model = WaitingCommandModel("import time; print('retained',flush=True); time.sleep(60)")
            async with Host(Path(temporary)/"state", model_api=model, allow_commands=True) as host:
                run, _ = await start_waiting(host, elapsed=3)
                command = next(iter(host.command_sessions.live))
                async with asyncio.timeout(6):
                    while True:
                        changed = host.run_signal(run.id)
                        if not host.command_sessions.has_live(run.id):
                            break
                        await changed.wait()
                saved = await host.command_sessions.observe(run.id, {"command_session_id":command})
                self.assertTrue(saved["process_stopped"])
                self.assertEqual(saved["termination"], "timeout")
                self.assertLessEqual(saved["timeout_seconds"], 3)
                self.assertEqual(model.calls, 1)

    async def test_wait_keeps_live_ownership_and_current_revocation_stops_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            component = os.environ.get("OEA_TEST_NATIVE_COMPONENT")
            native = patch("oea.workspace.native.executable", return_value=Path(component)) if component else None
            if native:
                native.start()
                self.addCleanup(native.stop)
            model = WaitingCommandModel("import time; print('retained',flush=True); time.sleep(60)")
            async with Host(Path(temporary) / "state", model_api=model, allow_commands=True) as host:
                run, definition = await start_waiting(host)
                value = await host.runs.get(run.id)
                self.assertTrue(value["in_flight"])
                self.assertFalse(value["stop_confirmed"])
                self.assertIn(run.id, host.active)
                command = next(iter(host.command_sessions.live))
                await host.agents.save(definition.model_copy(update={"tools":Tools()}), expected_revision=1)
                saved = await host.command_sessions.observe(run.id, {"command_session_id":command})
                self.assertTrue(saved["process_stopped"])
                self.assertEqual(saved["termination"], "permission_changed")
                value = await host.runs.get(run.id)
                self.assertEqual(value["state"], "waiting")
                self.assertFalse(value["in_flight"])
                self.assertTrue(value["stop_confirmed"])
                self.assertEqual(model.calls, 1)

    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
    async def test_abrupt_host_loss_reopens_original_receipts_without_replaying(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver = root / "host_driver.py"
            driver.write_text("""import asyncio,json,os,sys
from pathlib import Path
from unittest.mock import patch
from oea.runtime.host import Host
from test_command_recovery_v3 import WaitingCommandModel,start_waiting
async def main():
    command=\"import pathlib,time; p=pathlib.Path('starts.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('retained',flush=True); time.sleep(2); pathlib.Path('late.txt').write_text('late'); time.sleep(60)\"
    with patch('oea.workspace.native.executable',return_value=Path(os.environ['OEA_TEST_NATIVE_COMPONENT'])):
        async with Host(Path(sys.argv[1]),model_api=WaitingCommandModel(command),allow_commands=True) as host:
            run,_=await start_waiting(host)
            print(json.dumps({'run_id':run.id,'command':next(iter(host.command_sessions.live)),'work':str(host.active[run.id].paths.work_dir)}),flush=True)
            await asyncio.Event().wait()
asyncio.run(main())
""", encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
                str(Path(__file__).resolve().parent), environment.get("PYTHONPATH"))))
            process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-u", str(driver), str(root / "state"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment)
            try:
                line = await asyncio.wait_for(process.stdout.readline(), 15)
                if not line:
                    self.fail((await process.stderr.read()).decode(errors="replace"))
                identities = json.loads(line)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            work = Path(identities["work"])
            await asyncio.sleep(2.2)
            self.assertFalse((work / "late.txt").exists())
            model = WaitingCommandModel("raise AssertionError('must not replay')")
            async with Host(root / "state", model_api=model, allow_commands=True) as host:
                run = await host.runs.get(identities["run_id"])
                self.assertEqual(run["state"], "needs_review")
                self.assertFalse(run["stop_confirmed"])
                self.assertEqual(model.calls, 0)
                receipt = await host.runs.command(identities["run_id"], identities["command"], project_id="p")
                self.assertEqual(receipt["state"], "unknown")
                self.assertIn("retained", receipt["stdout"])
                with self.assertRaises(RuntimeFault):
                    await host.runs.resume(run["id"], "inspect-first")
                self.assertEqual((work / "starts.txt").read_text(), "1")
