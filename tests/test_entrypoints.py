"""Workbench operations exercise shared services through their public contracts."""

import json
from pathlib import Path
import tempfile
import unittest

from oea import AgentDefinition, Completion, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class TextModel:
    async def complete(self, *args, **options):
        return Completion(text="Done", usage={"prompt_tokens": 1, "completion_tokens": 1})


class EntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-entrypoints-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state", model_api=TextModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_workbench_management_and_archive_keep_original_receipts(self):
        call = self.host.registry.call
        self.assertEqual((await call("projects.update", {"project_id": "p", "name": "Renamed"}))["name"], "Renamed")
        session = await call("sessions.create", {"project_id": "p", "name": "Work"})
        self.assertEqual((await call("sessions.update", {"session_id": session["id"], "project_id": "p", "name": "Renamed work"}))["name"], "Renamed work")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid/v1", model="protocol-fixture")))
        instance = await self.host.instances.create(session["id"], "worker")
        spec = RunSpec(instance_id=instance["id"], input="Goal", idempotency_key="original")
        original = await self.host.runs.submit(spec)
        await self.host.runs.result(original.id, timeout=5)
        await call("sessions.set_archived", {"session_id": session["id"], "project_id": "p", "archived": True})
        before = await self.host.store.call(lambda db: {table: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"] for table in ("runs", "budgets", "actions", "events")})
        with self.assertRaises(RuntimeFault) as denied:
            await call("runs.submit", {"spec": {**spec.model_dump(mode="json"), "idempotency_key": "new"}})
        self.assertEqual(denied.exception.code, "SESSION_ARCHIVED")
        after = await self.host.store.call(lambda db: {table: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"] for table in before})
        self.assertEqual(before, after)
        replay = await call("runs.submit", {"spec": spec.model_dump(mode="json")})
        self.assertEqual(replay.id, original.id)
        self.assertTrue(replay.reused)
        self.assertEqual((await self.host.runs.result(original.id)).text, "Done")
        await call("sessions.set_archived", {"session_id": session["id"], "project_id": "p", "archived": False})
        resumed = await call("runs.submit", {"spec": {**spec.model_dump(mode="json"), "idempotency_key": "new"}})
        self.assertNotEqual(resumed.id, original.id)
        await self.host.runs.result(resumed.id, timeout=5)

    async def test_codex_import_and_removal_are_available_without_connecting(self):
        source = self.root / "config.toml"
        source.write_text('[mcp_servers.business]\ncommand = "python"\nargs = ["-m", "business"]\n', encoding="utf-8")
        call = self.host.registry.call
        candidates = await call("mcp.codex_candidates", {"project_id": "p", "config_path": str(source)})
        self.assertEqual(candidates["items"][0]["name"], "business")
        result = await call("mcp.import_codex", {"project_id": "p", "config_path": str(source), "server_names": ["business"]})
        saved = result["imported"][0]
        removed = await call("mcp.remove", {"server_id": saved["definition"]["id"], "project_id": "p", "expected_revision": saved["revision"]})
        self.assertTrue(removed["definition"]["removed"])
        self.assertEqual((await self.host.mcp.list("p"))["items"], [])
        self.assertEqual(self.host.mcp.connections, {})
        self.assertTrue(source.exists())

    async def test_imported_history_is_read_only_and_separate_from_new_work(self):
        for action in ("summary", "list", "get", "conversation", "usage"):
            guide = self.host.registry.describe("history." + action)
            self.assertTrue(guide["read_only"])
            self.assertIn("project_id", guide["parameters_schema"]["required"])
        summary = await self.host.registry.call("history.summary", {"project_id": "p"})
        self.assertTrue(summary["history_only"])
        self.assertFalse(summary["executable"])
        self.assertEqual(summary["tables"], [])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM runs")["count"]), 0)
