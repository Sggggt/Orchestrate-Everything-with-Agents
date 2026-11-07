"""Application-owned state survives waits, revocation and abrupt host loss."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, ApplicationBinding, ApplicationGrant, Model, Tools
from oea.contracts.applications import ApplicationRun, ApplicationResume, ApplicationResolution, ConversationCreate
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from test_command_recovery_v3 import WaitingCommandModel
from test_host_v2 import WaitModel


def process_terminated(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)
    try:
        return Path("/proc", str(pid), "stat").read_text().split(") ", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


async def setup_application(host, *, tools=Tools()):
    await host.projects.create("Example", project_id="p")
    await host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid", model="fixture"), tools=tools))
    registered = await host.applications.create("Example")
    identity = registered["application"]["id"]
    grant = ApplicationBinding(app_id=identity, project_id="p", agent_id="worker", grant=ApplicationGrant(tools=tools))
    binding = await host.applications.bind(grant)
    conversation = await host.oap.create_conversation(identity, ConversationCreate(binding_id=binding["id"]), "conversation")
    run = await host.oap.submit(identity, conversation["id"], ApplicationRun(input="Complete this work"), "work")
    async with asyncio.timeout(10):
        while True:
            signal = host.run_signal(run.id)
            value = await host.oap.run(identity, run.id)
            if value["state"] == "waiting":
                return registered, binding, conversation, run, grant
            if value["state"] in {"failed", "needs_review"}:
                raise AssertionError(value)
            await signal.wait()


class ApplicationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_resume_and_same_intent_after_reopen_keep_original_ownership_and_budget(self):
        with tempfile.TemporaryDirectory(prefix="oap-resume-") as temporary:
            root = Path(temporary) / "state"
            async with Host(root, model_api=WaitModel()) as host:
                registered, binding, conversation, run, grant = await setup_application(host)
                before = await host.instances.get(run.instance_id)
            model = WaitModel()
            async with Host(root, model_api=model) as host:
                identity = await host.applications.authenticate(registered["credential"])
                self.assertEqual((await host.oap.run(identity, run.id))["state"], "waiting")
                self.assertEqual(model.calls, 0)
                retry = await host.oap.submit(identity, conversation["id"], ApplicationRun(input="Complete this work"), "work")
                self.assertEqual(retry.id, run.id)
                self.assertTrue(retry.reused)
                await host.oap.resume(identity, run.id, ApplicationResume(input="37"), "reply")
                result = await host.runs.result(run.id, timeout=10)
                self.assertEqual(result.text, "Answer: 37")
                self.assertEqual(model.calls, 1)
                after = await host.instances.get(run.instance_id)
                self.assertEqual(before["workspace"]["work_dir"], after["workspace"]["work_dir"])
                self.assertIsNone(after["workspace"]["project_dir"])
                self.assertEqual((await host.oap.run(identity, run.id))["budget_id"], run.budget_id)
                self.assertFalse((await host.sessions.list("p"))["items"])

    async def test_revoked_binding_cannot_resume_after_restart_but_its_receipt_is_readable(self):
        with tempfile.TemporaryDirectory(prefix="oap-revoke-") as temporary:
            root = Path(temporary) / "state"
            async with Host(root, model_api=WaitModel()) as host:
                registered, binding, conversation, run, grant = await setup_application(host)
                await host.applications.bind(grant.model_copy(update={"enabled": False}), binding_id=binding["id"], expected_revision=1)
            model = WaitModel()
            async with Host(root, model_api=model) as host:
                identity = await host.applications.authenticate(registered["credential"])
                self.assertEqual((await host.oap.run(identity, run.id))["state"], "waiting")
                with self.assertRaises(RuntimeFault) as fault:
                    await host.oap.resume(identity, run.id, ApplicationResume(input="37"), "reply")
                self.assertEqual(fault.exception.code, "PERMISSION_DENIED")
                self.assertEqual(model.calls, 0)

    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
    async def test_cancel_confirms_native_tree_and_retains_original_output(self):
        with tempfile.TemporaryDirectory(prefix="oap-cancel-") as temporary:
            with patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"])):
                model = WaitingCommandModel("import time; print('original',flush=True); time.sleep(30)")
                async with Host(Path(temporary) / "state", model_api=model, allow_commands=True) as host:
                    registered, _, _, run, _ = await setup_application(host, tools=Tools(local=("exec_command", "write_stdin")))
                    command_id = next(iter(host.command_sessions.live))
                    async with asyncio.timeout(10):
                        while True:
                            observed = await host.command_sessions.observe(run.id, {"command_session_id": command_id, "yield_time_ms": 100})
                            if "original" in observed["stdout"]:
                                break
                    cancelled = await host.oap.cancel(registered["application"]["id"], run.id, "cancel")
                    self.assertEqual(cancelled["state"], "cancelled")
                    self.assertTrue(cancelled["stop_confirmed"])
                    self.assertFalse(host.command_sessions.live)
                    original = await host.oap.command(registered["application"]["id"], run.id, command_id)
                    self.assertTrue(original["process_stopped"])
                    self.assertIn("original", original["stdout"])

    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
    async def test_abrupt_loss_keeps_application_receipts_unknown_without_replaying(self):
        with tempfile.TemporaryDirectory(prefix="oap-crash-") as temporary:
            root = Path(temporary)
            driver = root / "driver.py"
            driver.write_text("""import asyncio,json,os,sys
from pathlib import Path
from unittest.mock import patch
from oea import Tools
from oea.runtime.host import Host
from test_application_recovery import setup_application
from test_command_recovery_v3 import WaitingCommandModel
async def main():
    code="from pathlib import Path; import os,time; p=Path('starts.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('started',os.getpid(),flush=True); time.sleep(2); Path('late.txt').write_text('late'); time.sleep(30)"
    with patch('oea.workspace.native.executable',return_value=Path(os.environ['OEA_TEST_NATIVE_COMPONENT'])):
        async with Host(Path(sys.argv[1]),model_api=WaitingCommandModel(code),allow_commands=True) as host:
            registered,_,conversation,run,_=await setup_application(host,tools=Tools(local=('exec_command','write_stdin')))
            command=next(iter(host.command_sessions.live))
            while True:
                receipt=await host.command_sessions.observe(run.id,{'command_session_id':command,'yield_time_ms':100})
                if 'started' in receipt['stdout']: break
            print(json.dumps({'run':run.id,'instance':run.instance_id,'conversation':conversation['id'],'app':registered['application']['id'],'credential':registered['credential'],'command':command,'work':str(host.active[run.id].paths.work_dir),'pid':os.getpid(),'command_pids':[receipt['pid'],receipt['workload_pid'],int(receipt['stdout'].split('started ',1)[1].split()[0])]}),flush=True)
            await asyncio.Event().wait()
asyncio.run(main())
""", encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join((str(Path(__file__).parent), str(Path(__file__).parents[1] / "src")))
            process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-u", str(driver), str(root / "state"), env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                line = await asyncio.wait_for(process.stdout.readline(), 15)
                if not line:
                    self.fail((await process.stderr.read()).decode(errors="replace"))
                identities = json.loads(line)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
            await asyncio.sleep(2.2)
            work = Path(identities["work"])
            self.assertFalse((work / "late.txt").exists())
            self.assertEqual((work / "starts.txt").read_text(), "1")
            self.assertTrue(all(process_terminated(pid) for pid in identities["command_pids"]))
            model = WaitModel()
            async with Host(root / "state", model_api=model, allow_commands=True) as host:
                identity = await host.applications.authenticate(identities["credential"])
                run = await host.oap.run(identity, identities["run"])
                self.assertEqual(run["state"], "needs_review")
                self.assertFalse(run["stop_confirmed"])
                self.assertEqual(model.calls, 0)
                receipt = await host.oap.command(identity, identities["run"], identities["command"])
                self.assertEqual(receipt["state"], "unknown")
                self.assertIn("started", receipt["stdout"])
                with self.assertRaises(RuntimeFault):
                    await host.oap.resume(identity, identities["run"], ApplicationResume(input="Continue"), "resume")
                self.assertFalse((await host.sessions.list("p"))["items"])
                resolution = ApplicationResolution(evidence="Observed the original process IDs terminated, one start marker, no late marker, and the retained stdout.",
                    outcome="completed", result={"command_session_id": identities["command"], "state": "exited", "process_stopped": True,
                        "exit_code": None, "isError": True, "termination": "host_disconnected", "stdout": receipt["stdout"], "stderr": receipt["stderr"]})
                resolved = await host.oap.confirm_action(identity, identities["run"], identities["command"], resolution, "verified")
                self.assertFalse(resolved["reused"])
                self.assertTrue((await host.oap.confirm_action(identity, identities["run"], identities["command"], resolution, "verified"))["reused"])
                action = await host.oap.action(identity, identities["run"], identities["command"])
                self.assertEqual(action["resolution"]["source"], "external_verification")
                self.assertTrue(action["result"]["process_stopped"])
                await host.oap.resume(identity, identities["run"], ApplicationResume(input="37"), "resume-verified")
                self.assertEqual((await host.runs.result(identities["run"], timeout=10)).text, "Answer: 37")
                self.assertEqual((work / "starts.txt").read_text(), "1")
                self.assertEqual(model.calls, 1)
