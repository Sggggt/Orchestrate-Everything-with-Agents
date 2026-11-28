"""Project-scoped views support the preserved workbench without old API aliases."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from model_support import register_model

from oea import AgentDefinition, Completion, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host


class TextModel:
    async def complete(self, model, messages, **options):
        return Completion(text="Evidence " * 2000, usage={"prompt_tokens": 20, "completion_tokens": 100})


class WorkbenchViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-workbench-views-")
        self.host = await Host(Path(self.temporary.name) / "state", model_api=TextModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        self.session = await self.host.sessions.create("p")
        await self.host.agents.save(AgentDefinition(id="writer", name="Writer", project_id="p", model_ref=await register_model(self.host, Model(base_url="https://model.invalid/v1", model="protocol-fixture"))))
        self.first = await self.host.instances.create(self.session["id"], "writer", name="First")
        self.second = await self.host.instances.create(self.session["id"], "writer", name="Second")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_instance_conversation_pages_are_bounded_and_reference_original_results(self):
        runs = await asyncio.gather(*(self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Goal", idempotency_key="work")) for instance in (self.first, self.second)))
        await asyncio.gather(*(self.host.runs.result(run.id, timeout=5) for run in runs))
        page = await self.host.registry.call("instances.history", {"instance_id": self.first["id"], "project_id": "p", "limit": 1})
        self.assertEqual(len(page["items"]), 1)
        answer = page["items"][0]
        self.assertEqual(answer["role"], "assistant")
        self.assertEqual(len(answer["text"]), 8192)
        self.assertEqual(answer["characters"], len("Evidence " * 2000))
        original = await self.host.runs.action(answer["action_id"], limit=16384)
        self.assertIn("Evidence ", original["text"])
        tail = await self.host.registry.call("instances.message", {"instance_id": self.first["id"], "project_id": "p",
            "entry_id": answer["id"], "offset": answer["next_offset"]})
        self.assertEqual(answer["text"] + tail["text"], "Evidence " * 2000)
        self.assertIsNone(tail["next_offset"])
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.instances.message(self.second["id"], "p", answer["id"])
        self.assertEqual(denied.exception.code, "NOT_FOUND")
        older = await self.host.instances.history(self.first["id"], "p", before=page["cursor"], limit=1)
        self.assertEqual(older["items"][0]["role"], "user")
        self.assertNotEqual(older["items"][0]["id"], answer["id"])
        self.assertIsNone(older["cursor"])
        filtered = await self.host.runs.list(self.session["id"], project_id="p", instance_id=self.first["id"])
        self.assertEqual([value["id"] for value in filtered["items"]], [runs[0].id])

    async def test_visible_message_pages_keep_activity_and_unicode_body_boundaries(self):
        run = await self.host.runs.submit(RunSpec(instance_id=self.first["id"], input="Goal", idempotency_key="work"))
        await self.host.runs.result(run.id, timeout=5)
        owner = {"id": run.id}
        await self.host.contexts.append(owner, self.first, {"role": "assistant", "content": None}, "tool-turn")
        text = "𐐷" * 8192 + "保存的完整内容"
        position = await self.host.contexts.append(owner, self.first, {"role": "user", "content": text}, "long-user")
        recent = await self.host.instances.history(self.first["id"], "p", limit=1, message_page=True)
        self.assertEqual([item["id"] for item in recent["items"]], ["long-user"])
        self.assertEqual(recent["cursor"], position)
        earlier = await self.host.instances.history(self.first["id"], "p", before=position, limit=1, message_page=True)
        self.assertEqual([item["id"] for item in earlier["items"]][:1], ["tool-turn"])
        self.assertEqual(len(earlier["items"]), 2)
        newest = await self.host.instances.history(self.first["id"], "p", after=earlier["cursor"], message_page=True)
        self.assertEqual(newest["items"], [*recent["items"], earlier["items"][0]])
        entry = recent["items"][0]
        body = await self.host.instances.message(self.first["id"], "p", entry["id"], offset=entry["next_offset"])
        self.assertEqual(entry["text"] + body["text"], text)

    async def test_summary_and_artifact_views_keep_session_and_project_ownership(self):
        run = await self.host.runs.submit(RunSpec(instance_id=self.first["id"], input="Goal", idempotency_key="work"))
        await self.host.runs.result(run.id, timeout=5)
        summary = await self.host.sessions.summary(self.session["id"], "p")
        self.assertEqual(summary["runs"], {"completed": 1})
        self.assertEqual(summary["artifacts"], 0)
        self.assertEqual((await self.host.artifacts.list(self.session["id"], "p"))["items"], [])
        for operation, params in (("sessions.summary", {"session_id": self.session["id"], "project_id": "q"}),
                                  ("artifacts.list", {"session_id": self.session["id"], "project_id": "q"}),
                                  ("instances.history", {"instance_id": self.first["id"], "project_id": "q"})):
            with self.subTest(operation=operation), self.assertRaises(RuntimeFault) as denied:
                await self.host.registry.call(operation, params)
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
