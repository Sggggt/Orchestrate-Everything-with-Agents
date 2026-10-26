"""Readiness reports observed configuration without executing business work."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from oea import AgentDefinition, Execution, Manager, MCPBinding, MCPServer, Model, SkillBinding, SkillSource, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class NoModel:
    async def complete(self, *args, **options):
        raise AssertionError("Configuration checks must not call a model")


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-readiness-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state", model_api=NoModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_role_credentials_are_independent_and_workspace_preview_has_no_effect(self):
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://main.invalid/v1", model="main"),
            manager=Manager(model=Model(base_url="https://manager.invalid/v1", model="manager", credential_ref="vault:missing")),
            workspace=Workspace(work=self.root / "work-not-created", shell=(sys.executable, "-B")), tools=Tools(local=("run_command",))))
        credential = "a synthetic readiness credential"
        await self.host.agents.set_credential("worker", "p", "main", credential, 1)
        before = await self.host.agents.get("worker")
        check = await self.host.registry.call("agents.check", {"agent_id": "worker", "project_id": "p"})
        self.assertEqual(check["model"]["state"], "configured")
        self.assertTrue(check["model"]["has_credential"])
        self.assertEqual(check["manager"]["model"]["missing"], ["credential"])
        self.assertFalse(check["manager"]["model"]["has_credential"])
        self.assertFalse(check["manager"]["business_tools"])
        self.assertFalse(check["model"]["connection_verified"])
        self.assertFalse(check["manager"]["model"]["connection_verified"])
        self.assertIn("run_command:host_grant", check["tools"]["unavailable"])
        self.assertTrue(check["executables"][0]["available"])
        self.assertEqual(check["workspace_check"]["state"], "resolved")
        self.assertFalse((self.root / "work-not-created").exists())
        self.assertNotIn(credential, json.dumps(check))
        self.assertEqual(await self.host.agents.get("worker"), before)
        for table in ("instances", "runs", "actions", "usage", "budgets"):
            self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"]), 0)

    async def test_check_reports_foreign_capabilities_missing_directories_and_programs(self):
        source = self.root / "guide"
        source.mkdir()
        (source / "SKILL.md").write_text("A bounded guide.", encoding="utf-8")
        await self.host.skills.register(SkillSource(id="guide", path=source, project_id="q"))
        await self.host.mcp.save(MCPServer(id="foreign", project_id="q", command=(sys.executable, "-B")))
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            tools=Tools(mcp=(MCPBinding(server_id="foreign"),)), skills=(SkillBinding(id="guide"),),
            workspace=Workspace(project=self.root / "missing-project", work=self.root / "uncreated-work"),
            execution=Execution(program=(str(self.root / "unavailable-program.exe"),))))
        check = await self.host.agents.check("worker", project_id="p")
        self.assertIn("skill:guide", check["tools"]["unavailable"])
        self.assertIn("mcp:foreign", check["tools"]["unavailable"])
        self.assertIn("program:program_unavailable", check["tools"]["unavailable"])
        self.assertEqual(check["workspace_check"]["issues"][0]["code"], "WORKSPACE_UNAVAILABLE")
        self.assertFalse((self.root / "uncreated-work").exists())
        self.assertEqual(self.host.mcp.connections, {})
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.agents.check("worker", project_id="q")
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

    async def test_invalid_write_grant_does_not_create_run_output_directory(self):
        from oea.workspace.service import WorkspacePaths
        project = self.root / "project"
        project.mkdir()
        settings = Workspace(project=project, work=self.root / "uncreated-work", writable_project_paths=("../escape",))
        with self.assertRaises(RuntimeFault):
            WorkspacePaths.prepare(settings, "instance", "run", self.host.root)
        self.assertFalse((self.root / "uncreated-work").exists())

    async def test_preview_uses_selected_project_folder_without_persisting_inherited_settings(self):
        project = self.root / "shared-project"
        project.mkdir()
        await self.host.projects.create("Folder project", folder=project, project_id="folder")
        await self.host.agents.save(AgentDefinition(id="shared", name="Shared", workspace=Workspace(work=self.root / "uncreated-work")))
        saved = await self.host.agents.get("shared")
        events_before = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events")["count"])
        check = await self.host.registry.call("agents.check", {"agent_id": "shared", "project_id": "folder"})
        self.assertEqual(check["workspace_check"]["paths"]["project_dir"], str(project.resolve()))
        self.assertIsNone(check["workspace"]["project"])
        self.assertEqual(check["workspace_check"]["state"], "resolved")
        self.assertEqual(await self.host.agents.get("shared"), saved)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events")["count"]), events_before)
        self.assertFalse((self.root / "uncreated-work").exists())
        for table in ("sessions", "instances", "runs", "actions", "usage", "budgets"):
            self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"]), 0)
        project.rmdir()
        unavailable = await self.host.agents.check("shared", project_id="folder")
        self.assertEqual(unavailable["workspace_check"]["state"], "needs_configuration")
        self.assertEqual(unavailable["workspace_check"]["issues"][0]["code"], "WORKSPACE_UNAVAILABLE")
        self.assertFalse((self.root / "uncreated-work").exists())
