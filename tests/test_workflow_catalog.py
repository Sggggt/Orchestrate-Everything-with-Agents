"""Preserved canvas catalog operations use the native workflow service."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from oea import WorkflowBuilder
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-workflow-catalog-")
        self.host = await Host(Path(self.temporary.name) / "state").__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        self.graph = WorkflowBuilder().node("start", "start", input_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}).node("end", "end", outputs={"value": "{{inputs.value}}"}).connect("start", "end").build()
        self.saved = await self.host.workflows.create("p", "Workflow", self.graph, description="A retained canvas definition")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_start_schema_rejects_inputs_before_accepting_run_or_budget(self):
        valid = await self.host.workflows.validate("p", self.graph.model_dump(mode="json"))
        self.assertTrue(valid["valid"])
        with self.assertRaises(RuntimeFault) as invalid:
            await self.host.workflows.run(self.saved["id"], {"value": "wrong"}, "invalid", project_id="p")
        self.assertEqual(invalid.exception.code, "INVALID_WORKFLOW_INPUT")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM workflow_runs")["count"]), 0)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM budgets")["count"]), 0)
        run = await self.host.workflows.run(self.saved["id"], {"value": 37}, "valid", project_id="p")
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=5), {"value": 37})
        view = await self.host.workflows.inspect(run["id"], project_id="p")
        self.assertEqual(view["graph"], self.graph.model_dump(mode="json"))

    async def test_incomplete_canvas_draft_can_be_saved_but_cannot_accept_work(self):
        saved = await self.host.workflows.create("p", "Draft", {"nodes": [], "edges": []})
        draft = {"nodes": [{"id": "worker", "type": "agent", "config": {"agent_id": ""}}], "edges": []}
        updated = await self.host.workflows.update(saved["id"], "p", 1, graph=draft)
        self.assertEqual((await self.host.workflows.get(saved["id"], "p"))["graph"], updated["graph"])
        report = await self.host.workflows.validate("p", updated["graph"])
        self.assertFalse(report["valid"])
        with self.assertRaises(RuntimeFault) as invalid:
            await self.host.workflows.run(saved["id"], {}, "unfinished", project_id="p")
        self.assertEqual(invalid.exception.code, "INVALID_WORKFLOW")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM workflow_runs")["count"]), 0)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM budgets")["count"]), 0)

    async def test_archived_session_rejects_new_work_but_retains_existing_run(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def held() -> int:
            started.set()
            await release.wait()
            return 37
        self.host.workflows.register_function(held)
        graph = (WorkflowBuilder().node("start", "start").node("work", "function", name="held")
                 .node("end", "end", outputs={"value": "{{nodes.work.value}}"})
                 .connect("start", "work").connect("work", "end").build())
        saved = await self.host.workflows.create("p", "Existing work", graph)
        session = await self.host.sessions.create("p")
        original = await self.host.workflows.run(saved["id"], {}, "original", project_id="p", session_id=session["id"])
        try:
            await asyncio.wait_for(started.wait(), 5)
            await self.host.sessions.set_archived(session["id"], "p", True)
            counts = await self.host.store.call(lambda db: (
                one(db, "SELECT COUNT(*) AS count FROM workflow_runs")["count"],
                one(db, "SELECT COUNT(*) AS count FROM budgets")["count"]))
            with self.assertRaises(RuntimeFault) as archived:
                await self.host.workflows.run(saved["id"], {}, "new-work", project_id="p", session_id=session["id"])
            self.assertEqual(archived.exception.code, "SESSION_ARCHIVED")
            replay = await self.host.workflows.run(saved["id"], {}, "original", project_id="p", session_id=session["id"])
            self.assertTrue(replay["reused"])
            self.assertEqual(replay["id"], original["id"])
            self.assertEqual(await self.host.store.call(lambda db: (
                one(db, "SELECT COUNT(*) AS count FROM workflow_runs")["count"],
                one(db, "SELECT COUNT(*) AS count FROM budgets")["count"])), counts)
            current = await self.host.workflows.get_run(original["id"], project_id="p")
            self.assertEqual(current["state"], "running")
            self.assertIsNone(current["cancel_requested_at"])
        finally:
            release.set()
        self.assertEqual(await self.host.workflows.result(original["id"], project_id="p", timeout=5), {"value": 37})

    async def test_archive_recycle_and_restore_retain_definition_and_history(self):
        archived = await self.host.workflows.set_archived(self.saved["id"], "p", True, 1)
        self.assertEqual(archived["state"], "archived")
        self.assertEqual((await self.host.workflows.list("p"))["items"], [])
        self.assertEqual(len((await self.host.workflows.list("p", include_archived=True))["items"]), 1)
        with self.assertRaises(RuntimeFault) as inactive:
            await self.host.workflows.run(self.saved["id"], {"value": 1}, "archived-run", project_id="p")
        self.assertEqual(inactive.exception.code, "WORKFLOW_NOT_ACTIVE")
        restored = await self.host.workflows.set_archived(self.saved["id"], "p", False, 2)
        run = await self.host.workflows.run(restored["id"], {"value": 37}, "work", project_id="p")
        await self.host.workflows.result(run["id"], project_id="p", timeout=5)
        deleted = await self.host.workflows.remove(restored["id"], "p", 3, confirmed=True)
        self.assertEqual(deleted["state"], "deleted")
        self.assertEqual(len((await self.host.workflows.list("p", deleted_only=True))["items"]), 1)
        final = await self.host.workflows.restore(restored["id"], "p", 4)
        self.assertEqual(final["graph"], self.saved["graph"])
        self.assertEqual(final["description"], self.saved["description"])
        self.assertEqual(final["revision"], 5)
        self.assertEqual((await self.host.workflows.get_run(run["id"], project_id="p"))["state"], "completed")
        with self.assertRaises(RuntimeFault) as foreign:
            await self.host.workflows.set_archived(final["id"], "q", True, 5)
        self.assertEqual(foreign.exception.code, "PERMISSION_DENIED")

    async def test_waiting_work_prevents_removal_and_live_activity_has_actual_count(self):
        graph = WorkflowBuilder().node("start", "start").node("confirm", "wait", prompt="Confirm").node("end", "end", outputs={"ok": True}).connect("start", "confirm").connect("confirm", "end").build()
        saved = await self.host.workflows.create("p", "Human wait", graph)
        run = await self.host.workflows.run(saved["id"], {}, "wait", project_id="p")
        async with asyncio.timeout(5):
            while True:
                changed = self.host.workflows.signal(run["id"])
                if (await self.host.workflows.get_run(run["id"], project_id="p"))["state"] == "waiting":
                    break
                await changed.wait()
        activity = await self.host.workflows.activity("p")
        self.assertEqual(next(value["active_count"] for value in activity["items"] if value["workflow_id"] == saved["id"]), 1)
        with self.assertRaises(RuntimeFault) as busy:
            await self.host.workflows.remove(saved["id"], "p", 1, confirmed=True)
        self.assertEqual(busy.exception.code, "WORKFLOW_IN_USE")
        await self.host.workflows.cancel(run["id"], project_id="p")
        self.assertEqual((await self.host.workflows.remove(saved["id"], "p", 1, confirmed=True))["state"], "deleted")
