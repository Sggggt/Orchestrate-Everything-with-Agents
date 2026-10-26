"""Persisted reader cursors, aggregate unread counts and source identities."""

import json
import tempfile
import unittest
from pathlib import Path

from oea import AgentDefinition, Completion, Manager, Model, RunSpec, MessageSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class ModelFixture:
    async def complete(self, model, messages, **options):
        return Completion(text="Saved reply", usage={"prompt_tokens": 10, "completion_tokens": 2})


class UnreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-reading-")
        self.root = Path(self.temporary.name) / "state"
        self.host = await Host(self.root, model_api=ModelFixture()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        model = Model(base_url="https://example.invalid/v1", model="fixture")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=model, manager=Manager(model=model)))
        self.session = await self.host.sessions.create("p")
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        self.first = await self.host.instances.create(self.session["id"], "worker", name="first")
        self.second = await self.host.instances.create(self.session["id"], "worker", name="second")
        self.manager = await self.host.instances.create(self.session["id"], "worker", name="manager", role="manager")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def reply(self, instance, key):
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Reply", idempotency_key=key))
        await self.host.runs.result(run.id, timeout=10)
        history = await self.host.instances.history(instance["id"], "p", limit=1)
        return history["items"][0]["position"]

    async def unread(self, reader="reader-a"):
        return await self.host.sessions.unread(self.session["id"], "p", consumer_id=reader)

    async def test_counts_aggregate_all_instances_and_keep_readers_independent(self):
        await self.reply(self.first, "first-1")
        latest = await self.reply(self.first, "first-2")
        await self.reply(self.second, "second")
        await self.reply(self.manager, "manager")
        self.assertEqual((await self.unread())["agents"]["worker"], {"instances": 3, "count": 4})
        read = await self.host.instances.mark_read(self.first["id"], "p", latest, consumer_id="reader-a")
        self.assertEqual(read["summary"]["agents"]["worker"]["count"], 2)
        self.assertEqual((await self.unread("reader-b"))["agents"]["worker"]["count"], 4)
        replay = await self.host.instances.mark_read(self.first["id"], "p", 0, consumer_id="reader-a")
        self.assertEqual(replay["position"], latest)
        self.assertEqual(replay["summary"]["revision"], read["summary"]["revision"])
        await self.host.aclose()
        self.host = await Host(self.root, model_api=ModelFixture()).__aenter__()
        self.assertEqual((await self.unread())["agents"]["worker"]["count"], 2)
        page = await self.host.instances.list(self.session["id"], consumer_id="reader-a")
        self.assertEqual(next(item for item in page["items"] if item["id"] == self.first["id"])["unread_count"], 0)

    async def test_cursors_reject_cross_instance_project_and_reader(self):
        position = await self.reply(self.first, "one")
        with self.assertRaises(RuntimeFault):
            await self.host.instances.mark_read(self.second["id"], "p", position, consumer_id="reader-a")
        with self.assertRaises(RuntimeFault):
            await self.host.instances.mark_read(self.first["id"], "another-project", position, consumer_id="reader-a")
        with self.assertRaises(RuntimeFault):
            await self.host.registry.call("instances.mark_read", {"instance_id": self.first["id"], "project_id": "p", "position": position, "consumer_id": "reader-b"}, consumer_id="reader-a")

    async def test_group_reads_do_not_clear_instance_replies(self):
        await self.reply(self.first, "one")
        spec = MessageSpec(session_id=self.session["id"], recipients=(self.first["id"],), content="Information", idempotency_key="message")
        message = await self.host.registry.call("messages.send", {"spec": spec.model_dump(mode="json")}, consumer_id="external", agent_name="codex")
        self.assertEqual(message["source"]["kind"], "leader")
        self.assertEqual(message["source"]["name"], "codex")
        replay = await self.host.registry.call("messages.send", {"spec": spec.model_dump(mode="json")}, consumer_id="external", agent_name="another-agent")
        self.assertEqual(replay["source"]["name"], "codex")
        read = await self.host.messages.mark_read(self.session["id"], "reader-a", message["position"], project_id="p")
        self.assertEqual(read["summary"]["messages"]["count"], 0)
        self.assertEqual(read["summary"]["agents"]["worker"]["count"], 1)

    async def test_summary_is_not_limited_to_the_first_instance_page(self):
        for index in range(100):
            last = await self.host.instances.create(self.session["id"], "worker", name="extra-" + str(index))
        await self.reply(last, "last")
        page = await self.host.instances.list(self.session["id"], limit=100, consumer_id="reader-a")
        self.assertEqual(sum(item["unread_count"] for item in page["items"]), 0)
        self.assertEqual((await self.unread())["agents"]["worker"], {"instances": 103, "count": 1})

    async def test_empty_assistant_activity_does_not_become_an_unread_message(self):
        await self.reply(self.first, "one")
        run = await self.host.store.call(lambda db: one(db, "SELECT id FROM runs WHERE instance_id=?", (self.first["id"],)))
        await self.host.contexts.append(run, self.first, {"role": "assistant", "content": None}, "activity")
        self.assertEqual((await self.unread())["agents"]["worker"]["count"], 1)

    async def test_project_deletion_removes_reader_cursors(self):
        position = await self.reply(self.first, "one")
        await self.host.instances.mark_read(self.first["id"], "p", position, consumer_id="reader-a")
        await self.host.projects.delete("p", "Project", "delete")
        count = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM instance_read_positions"))
        self.assertEqual(count["count"], 0)
