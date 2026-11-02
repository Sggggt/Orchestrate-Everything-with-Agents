"""Inherited environment snapshots and current grants remain one execution contract."""

import asyncio
import sys
import tempfile
from pathlib import Path
import unittest

from oea import AgentDefinition, Model, ProjectWorkspace, RunSpec, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.workspace.environment import ExecutionEnvironment
from test_host_v2 import BarrierModel


class ProjectEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.model = BarrierModel()
        self.host = await Host(self.root / "state", allow_commands=True, model_api=self.model).__aenter__()
        await self.host.projects.create("Project", project_id="p", workspace=ProjectWorkspace(
            references=(self.shared,), writable_directories=(self.shared,), network="allow", environment={"SHARED_SETTING":"present"}))
        self.session = await self.host.sessions.create("p")

    async def asyncTearDown(self):
        self.model.release.set()
        await self.host.aclose()
        self.temporary.cleanup()

    async def active(self, workspace=None):
        definition = AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid", model="test"), tools=Tools(local=("exec_command", "write_stdin")),
            workspace=workspace or Workspace(sandbox="trusted"))
        await self.host.agents.save(definition)
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Inspect environment", idempotency_key="once"))
        await asyncio.wait_for(self.model.ready.wait(), 5)
        return run, definition

    async def test_inherited_grants_and_network_are_snapshotted(self):
        run, _ = await self.active()
        paths, settings = await self.host.resolve_workspace(run.id)
        self.assertEqual(settings.network, "allow")
        self.assertEqual(settings.environment, {"SHARED_SETTING":"present"})
        self.assertEqual(paths.resolve(str(self.shared / "result.txt"), write=True), self.shared / "result.txt")
        self.assertFalse(settings.inherit_project)

    async def test_agent_can_disable_inheritance_and_choose_a_narrower_policy(self):
        run, _ = await self.active(Workspace(sandbox="trusted", inherit_project=False, network="deny"))
        paths, settings = await self.host.resolve_workspace(run.id)
        self.assertEqual(settings.network, "deny")
        self.assertEqual(settings.environment, {})
        with self.assertRaises(RuntimeFault):
            paths.resolve(str(self.shared / "result.txt"), write=True)

    async def test_project_revocation_stops_original_command_without_replaying_it(self):
        run, _ = await self.active()
        paths, settings = await self.host.resolve_workspace(run.id)
        receipt = await self.host.command_sessions.start({"run_id":run.id, "action_id":"command-revocation",
            "arguments":{"argv":[sys.executable,"-B","-c","import time; print('original',flush=True); time.sleep(30)"],
                         "yield_time_ms":250}}, ExecutionEnvironment(paths, settings))
        self.assertEqual(receipt["state"], "running")
        project = await self.host.projects.get("p")
        await self.host.projects.update("p", "Project", workspace=ProjectWorkspace(), expected_revision=project["revision"])
        saved = await self.host.command_sessions.observe(run.id, {"command_session_id":receipt["command_session_id"]})
        self.assertTrue(saved["process_stopped"])
        self.assertEqual(saved["termination"], "permission_changed")
        self.assertIn("original", saved["stdout"])
        with self.assertRaises(RuntimeFault) as failure:
            await self.host.projects.update("p", "Project", workspace=ProjectWorkspace(), expected_revision=project["revision"])
        self.assertEqual(failure.exception.code, "REVISION_CONFLICT")
