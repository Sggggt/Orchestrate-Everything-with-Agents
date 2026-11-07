"""Registration deletion withdraws grants while retaining original evidence."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from oea import AgentDefinition, Completion, MCPServer, Model, RunSpec, SkillSource
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows


class ModelFixture:
    async def complete(self, model, messages, **options):
        return Completion(text="Complete", usage={"prompt_tokens": 20, "completion_tokens": 2})


class RegistrationRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-remove-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state", model_api=ModelFixture()).__aenter__()
        for project in ("p", "q"):
            await self.host.projects.create(project, project_id=project)
        self.session = await self.host.sessions.create("p")
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid/v1", model="fixture"))
        await self.host.agents.save(self.definition)

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_agent_removal_hides_registration_and_preserves_completed_history_after_restart(self):
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Inspect", idempotency_key="work"))
        await self.host.runs.result(run.id, timeout=5)
        receipt = await self.host.registry.call("agents.remove", {"agent_id": "worker", "project_id": "p", "expected_revision": 1})
        self.assertTrue(receipt["removed"])
        self.assertEqual((await self.host.agents.list("p"))["items"], [])
        self.assertNotIn("worker", self.host.definitions)
        self.assertEqual((await self.host.runs.result(run.id)).text, "Complete")
        again = await self.host.agents.remove("worker", "p", 1)
        self.assertTrue(again["reused"])
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="New work", idempotency_key="new"))
        self.assertEqual(denied.exception.code, "AGENT_DISABLED")
        with self.assertRaises(RuntimeFault) as resurrect:
            await self.host.agents.save(self.definition, expected_revision=2)
        self.assertEqual(resurrect.exception.code, "AGENT_REMOVED")
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=ModelFixture()).__aenter__()
        self.assertNotIn("worker", self.host.definitions)
        self.assertEqual((await self.host.agents.list("p"))["items"], [])
        self.assertTrue((await self.host.agents.list("p", include_removed=True))["items"][0]["definition"]["removed"])
        self.assertEqual((await self.host.runs.result(run.id)).text, "Complete")
        self.assertEqual(len((await self.host.instances.list(self.session["id"]))["items"]), 1)

    async def test_wrong_project_or_revision_does_not_remove_agent(self):
        for project, revision, code in (("q", 1, "PERMISSION_DENIED"), ("p", 0, "REVISION_CONFLICT")):
            with self.assertRaises(RuntimeFault) as rejected:
                await self.host.agents.remove("worker", project, revision)
            self.assertEqual(rejected.exception.code, code)
        self.assertFalse((await self.host.agents.get("worker"))["definition"]["removed"])
        self.assertEqual((await self.host.agents.get("worker"))["revision"], 1)

    async def test_waiting_work_blocks_agent_deletion_until_actual_cancellation(self):
        class WaitingModel:
            async def complete(self, model, messages, **options):
                return Completion(tool_calls=({"id": "wait", "type": "function", "function": {"name": "wait_for_input", "arguments": '{"prompt":"Continue"}'}},),
                                  finish_reason="tool_calls", usage={"prompt_tokens": 20, "completion_tokens": 3})
        self.host.model_api = WaitingModel()
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Wait", idempotency_key="waiting"))
        async with asyncio.timeout(5):
            while True:
                changed = self.host.run_signal(run.id)
                if (await self.host.runs.get(run.id))["state"] == "waiting":
                    break
                await changed.wait()
        with self.assertRaises(RuntimeFault) as busy:
            await self.host.agents.remove("worker", "p", 1)
        self.assertEqual(busy.exception.code, "AGENT_IN_USE")
        self.assertEqual(busy.exception.details["id"], run.id)
        await self.host.runs.cancel(run.id)
        async with asyncio.timeout(5):
            while (await self.host.runs.get(run.id))["state"] != "cancelled":
                await self.host.run_signal(run.id).wait()
        self.assertTrue((await self.host.agents.remove("worker", "p", 1))["removed"])

    async def test_missing_skill_source_can_be_removed_and_original_body_is_retained(self):
        source = self.root / "skill"
        source.mkdir()
        (source / "SKILL.md").write_text("Read the evidence.", encoding="utf-8")
        await self.host.skills.register(SkillSource(id="guide", project_id="p", path=source))
        (source / "SKILL.md").unlink()
        source.rmdir()
        removed = await self.host.registry.call("skills.remove", {"skill_id": "guide", "project_id": "p", "expected_revision": 1})
        self.assertTrue(removed["removed"])
        self.assertEqual((await self.host.skills.list("p"))["items"], [])
        self.assertNotIn("guide", self.host.skills.definitions)
        self.assertEqual((await self.host.skills.body("guide", 1))["body"], "Read the evidence.")
        again = await self.host.skills.remove("guide", "p", 1)
        self.assertTrue(again["reused"])
        self.assertEqual(again["revision"], 2)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE kind='skill.removed'")["n"]), 1)

    async def test_mcp_removal_does_not_start_missing_service_and_replays_original_revision(self):
        await self.host.mcp.save(MCPServer(id="service", project_id="p", command=("not-installed-program",), source_directory=self.root / "missing-source"))
        removed = await self.host.registry.call("mcp.remove", {"server_id": "service", "project_id": "p", "expected_revision": 1})
        self.assertTrue(removed["definition"]["removed"])
        self.assertEqual((await self.host.mcp.list("p"))["items"], [])
        self.assertEqual(self.host.mcp.connections, {})
        self.assertEqual(await self.host.mcp.remove("service", "p", 1), removed)
        self.assertEqual((await self.host.mcp.list("p", include_removed=True))["items"][0]["revision"], 2)
        events = await self.host.store.call(lambda db: rows(db, "SELECT data_json FROM events WHERE kind='mcp.saved' ORDER BY position"))
        self.assertIn('"removed":true', events[-1]["data_json"])

    async def test_removed_metadata_cannot_be_written_through_save(self):
        with self.assertRaises(RuntimeFault):
            await self.host.agents.save(self.definition.model_copy(update={"removed": True, "enabled": False}))
        self.assertEqual((await self.host.agents.get("worker"))["revision"], 1)
