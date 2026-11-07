"""Board persistence, private readers and incremental delivery with real host state."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from oea import AgentDefinition, Communication, Completion, Manager, MessageSpec, Model, RunSpec, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows


class ModelFixture:
    def __init__(self):
        self.requests = []
        self.started, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, model, messages, **kwargs):
        self.requests.append({"model": model.model, "messages": messages, "tools": kwargs.get("tools", [])})
        self.started.set()
        await self.release.wait()
        return Completion(text="Confirmed", usage={"prompt_tokens": 50, "completion_tokens": 5})


class MessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-messages-")
        self.root = Path(self.temporary.name)
        self.model = ModelFixture()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://example.invalid/v1", model="main-fixture"),
            manager=Manager(model=Model(base_url="https://example.invalid/v1", model="manager-fixture"))))
        self.session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(self.session["id"], "worker", name="first")
        self.second = await self.host.instances.create(self.session["id"], "worker", name="second")
        boards = (await self.host.boards.list("p", session_id=self.session["id"]))["items"]
        self.board = next(value["id"] for value in boards if value["scope"] == "session")
        self.inbox = next(value["id"] for value in boards if value["owner_instance_id"] == self.second["id"])

    async def asyncTearDown(self):
        self.model.release.set()
        await self.host.aclose()
        self.temporary.cleanup()

    def spec(self, key, content="Information", **options):
        return MessageSpec(board_id=options.pop("board_id", self.board), content=content, idempotency_key=key, **options)

    async def active(self, instance=None, key="work"):
        self.model.started.clear()
        run = await self.host.runs.submit(RunSpec(instance_id=(instance or self.instance)["id"], input="Perform authorized work", idempotency_key=key))
        await asyncio.wait_for(self.model.started.wait(), 10)
        return run

    async def test_body_survives_invalid_optional_fields_and_never_starts_work(self):
        spec = self.spec("notice", "@all path=/wrong business={broken}", mentions=(self.instance["id"], "missing", "all"),
                         reply_to="absent", context_refs=("missing-source",), artifact_ids=("missing-artifact",))
        first = await self.host.messages.send(spec)
        self.assertEqual(first["content"], spec.content)
        self.assertEqual(len(first["issues"]), 5)
        self.assertEqual([item["instance_id"] for item in first["deliveries"]], [self.instance["id"]])
        self.assertTrue((await self.host.messages.send(spec))["reused"])
        self.assertEqual(self.model.requests, [])
        counts = await self.host.store.call(lambda db: [one(db, "SELECT COUNT(*) AS count FROM " + name)["count"] for name in ("runs", "budgets")])
        self.assertEqual(counts, [0, 0])
        self.assertEqual(len((await self.host.messages.list(self.board, project_id="p"))["items"]), 1)
        with self.assertRaises(RuntimeFault):
            await self.host.messages.send(self.spec("notice", "changed"))

    async def test_private_readers_are_explicit_and_mentions_cannot_expand_them(self):
        run = await self.active()
        sent = await self.host.tools.invoke(run.id, "send_message", {"board_id": self.inbox, "content": "Private evidence", "mentions": [self.instance["id"]], "idempotency_key": "private"}, "private")
        self.assertEqual([item["instance_id"] for item in sent["deliveries"]], [self.second["id"]])
        self.assertEqual(sent["issues"][0]["reason"], "NOT_DELIVERED")
        denied = await self.host.tools.invoke(run.id, "read_messages", {"board_id": self.inbox}, "read-denied")
        self.assertTrue(denied["isError"])
        await self.host.boards.set_reader(self.inbox, self.instance["id"], True, project_id="p")
        page = await self.host.tools.invoke(run.id, "read_messages", {"board_id": self.inbox}, "read-granted")
        self.assertEqual(page["items"][0]["content"], "Private evidence")
        await self.host.boards.set_reader(self.inbox, self.instance["id"], False, project_id="p")
        with self.assertRaises(RuntimeFault):
            await self.host.tools.invoke(run.id, "read_messages", {"board_id": self.inbox}, "read-granted")
        self.assertEqual(len((await self.host.messages.list(self.inbox, project_id="p"))["items"]), 1)

    async def test_session_feed_merges_boards_with_one_ordered_cursor_and_keeps_scope(self):
        first = await self.host.messages.send(self.spec("general-one", "General one", mentions=(self.instance["id"],)))
        private = await self.host.messages.send(self.spec("private-one", "Private one", board_id=self.inbox))
        last = await self.host.messages.send(self.spec("general-two", "General two"))
        other_session = await self.host.sessions.create("p")
        other_board = next(item for item in (await self.host.boards.list("p", session_id=other_session["id"]))["items"] if item["scope"] == "session")
        await self.host.messages.send(MessageSpec(board_id=other_board["id"], content="Other session", idempotency_key="other"))
        page = await self.host.registry.call("messages.list", {"session_id": self.session["id"], "project_id": "p", "order": "desc", "limit": 2})
        self.assertEqual([item["id"] for item in page["items"]], [last["id"], private["id"]])
        self.assertEqual(page["items"][1]["board"]["scope"], "inbox")
        self.assertEqual(page["items"][1]["board"]["owner_name"], "second")
        earlier = await self.host.messages.list(project_id="p", session_id=self.session["id"], order="desc", before=page["cursor"], limit=2)
        self.assertEqual([item["id"] for item in earlier["items"]], [first["id"]])
        self.assertEqual(earlier["items"][0]["deliveries"][0]["instance_name"], "first")
        self.assertIsNone(earlier["cursor"])
        for params in ({"session_id": self.session["id"], "project_id": "other"},
                       {"board_id": self.board, "session_id": self.session["id"], "project_id": "p"}, {"project_id": "p"}):
            with self.subTest(params=params), self.assertRaises(RuntimeFault):
                await self.host.messages.list(**params)
        with self.assertRaises(RuntimeFault):
            await self.host.store.call(lambda db: self.host.messages.page(db, None, project_id="p", session_id=self.session["id"], instance=self.instance))

    async def test_session_read_positions_batch_boards_without_marking_agent_deliveries(self):
        first = await self.host.messages.send(self.spec("general-one", mentions=(self.instance["id"],)))
        private = await self.host.messages.send(self.spec("private-one", board_id=self.inbox))
        last = await self.host.messages.send(self.spec("general-two"))
        unread = await self.host.messages.unread(None, "reader-a", project_id="p", session_id=self.session["id"])
        self.assertEqual(unread["count"], 3)
        read = await self.host.messages.mark_read(None, "reader-a", private["position"], project_id="p", session_id=self.session["id"])
        self.assertEqual(read["positions"], {self.board: first["position"], self.inbox: private["position"]})
        self.assertEqual(read["summary"]["messages"]["count"], 1)
        self.assertEqual((await self.host.messages.unread(None, "reader-b", project_id="p", session_id=self.session["id"]))["count"], 3)
        replay = await self.host.messages.mark_read(None, "reader-a", first["position"], project_id="p", session_id=self.session["id"])
        self.assertEqual(replay["positions"], read["positions"])
        await self.host.messages.mark_read(None, "reader-a", last["position"], project_id="p", session_id=self.session["id"])
        self.assertEqual((await self.host.messages.unread(None, "reader-a", project_id="p", session_id=self.session["id"]))["count"], 0)
        self.assertIsNone((await self.host.messages.get(first["id"], project_id="p"))["deliveries"][0]["read_at"])
        self.assertIsNone((await self.host.messages.get(private["id"], project_id="p"))["deliveries"][0]["read_at"])
        other = await self.host.sessions.create("p")
        board = next(item for item in (await self.host.boards.list("p", session_id=other["id"]))["items"] if item["scope"] == "session")
        foreign = await self.host.messages.send(MessageSpec(board_id=board["id"], content="Other", idempotency_key="other"))
        with self.assertRaises(RuntimeFault):
            await self.host.messages.mark_read(None, "reader-a", foreign["position"], project_id="p", session_id=self.session["id"])
        self.assertEqual((await self.host.messages.unread(None, "reader-a", project_id="p", session_id=other["id"]))["count"], 1)

    async def test_members_and_boards_return_stable_scope_without_creating_manager(self):
        run = await self.active()
        members = await self.host.tools.invoke(run.id, "list_members", {}, "members")
        self.assertEqual({value["id"] for value in members["items"]}, {self.instance["id"], self.second["id"]})
        self.assertTrue(all(not value["can_assign"] for value in members["items"]))
        self.assertEqual(next(value for value in members["items"] if value["id"] == self.second["id"])["inbox_board_id"], self.inbox)
        boards = await self.host.tools.invoke(run.id, "list_boards", {}, "boards")
        self.assertNotIn(self.inbox, {value["id"] for value in boards["items"]})
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM instances WHERE role='manager'")["count"]), 0)

    async def test_incremental_cursor_survives_host_restart_and_repeat_read(self):
        first = await self.host.messages.send(self.spec("first"))
        second = await self.host.messages.send(self.spec("second"))
        run = await self.active()
        page = await self.host.tools.invoke(run.id, "read_messages", {"board_id": self.board, "limit": 1}, "page")
        self.assertEqual([value["id"] for value in page["items"]], [first["id"]])
        repeated = await self.host.tools.invoke(run.id, "read_messages", {"board_id": self.board, "limit": 1}, "page")
        self.assertEqual(repeated, page)
        self.model.release.set()
        await self.host.runs.result(run.id, timeout=10)
        await self.host.aclose()
        self.model = ModelFixture()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        run = await self.active(key="after-restart")
        page = await self.host.tools.invoke(run.id, "read_messages", {"board_id": self.board}, "restored-cursor")
        self.assertEqual([value["id"] for value in page["items"]], [second["id"]])

    async def test_notifications_merge_once_and_controller_read_does_not_mark_agent_read(self):
        first = await self.host.messages.send(self.spec("one", mentions=(self.instance["id"],)))
        await self.host.messages.send(self.spec("two", mentions=(self.instance["id"],)))
        await self.host.messages.mark_read(self.board, "controller", first["position"], project_id="p")
        self.assertIsNone((await self.host.messages.get(first["id"], project_id="p"))["deliveries"][0]["read_at"])
        run = await self.active()
        context = self.host.active[run.id]
        self.assertEqual(await self.host.contexts.inbox(context.run, context.instance), 0)
        received = await self.host.store.call(lambda db: rows(db, "SELECT message_id FROM conversation WHERE run_id=? AND message_id IS NOT NULL", (run.id,)))
        self.assertEqual(len(received), 2)
        third = await self.host.messages.send(self.spec("three", "Correction: correct-path", mentions=(self.instance["id"],)))
        self.assertEqual(await self.host.contexts.inbox(context.run, context.instance), 1)
        self.assertEqual(await self.host.contexts.inbox(context.run, context.instance), 0)
        self.assertIsNotNone((await self.host.messages.get(third["id"], project_id="p"))["deliveries"][0]["read_at"])
        self.assertEqual(len(self.model.requests), 1)

    async def test_revoked_private_read_grant_blocks_pending_injection(self):
        await self.host.boards.set_reader(self.inbox, self.instance["id"], True, project_id="p")
        run = await self.active()
        sent = await self.host.messages.send(self.spec("private", board_id=self.inbox, mentions=(self.instance["id"],)))
        await self.host.boards.set_reader(self.inbox, self.instance["id"], False, project_id="p")
        context = self.host.active[run.id]
        self.assertEqual(await self.host.contexts.inbox(context.run, context.instance), 0)
        receipt = await self.host.messages.get(sent["id"], project_id="p")
        self.assertTrue(all(value["read_at"] is None for value in receipt["deliveries"]))

    async def test_cross_project_mentions_fail_without_losing_body(self):
        await self.host.projects.create("Other", project_id="q")
        session = await self.host.sessions.create("q")
        await self.host.agents.save(AgentDefinition(id="other", name="Other", project_id="q"))
        other = await self.host.instances.create(session["id"], "other")
        sent = await self.host.messages.send(self.spec("cross", mentions=(other["id"],)))
        self.assertEqual(sent["deliveries"], [])
        self.assertEqual(sent["issues"][0]["reason"], "NOT_DELIVERED")
        with self.assertRaises(RuntimeFault):
            await self.host.messages.get(sent["id"], project_id="q")

    async def test_explicit_source_share_retains_revocation_and_does_not_grant_origin_tool(self):
        run = await self.active()
        context = self.host.active[run.id]
        reference = await self.host.contexts.source(context.run, context.instance, "Verified evidence", "tool:exec_command")
        spec = self.spec("share", board_id=self.inbox, context_refs=(reference,))
        await self.host.messages.send(spec)
        target = await self.host.store.call(lambda db: one(db, "SELECT * FROM instances WHERE id=?", (self.second["id"],)))
        async def no_commands(*args):
            raise RuntimeFault("PERMISSION_DENIED", "No origin tool grant")
        value = await self.host.contexts.read({"id": "reader"}, target, reference, authorize=no_commands)
        self.assertEqual(value["text"], "Verified evidence")
        await self.host.contexts.revoke(reference, target["id"], project_id="p")
        await self.host.messages.send(spec)
        with self.assertRaises(RuntimeFault):
            await self.host.contexts.read({"id": "reader"}, target, reference, authorize=no_commands)

    async def test_manager_inherits_business_tools_and_explicit_override_can_narrow(self):
        saved = await self.host.agents.get("worker")
        definition = AgentDefinition.model_validate(saved["definition"])
        await self.host.agents.save(definition.model_copy(update={"tools": Tools(local=("apply_patch",)), "workspace": Workspace(sandbox="trusted")}), expected_revision=saved["revision"])
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        manager = await self.host.instances.create(self.session["id"], "worker", role="manager")
        run = await self.active(manager)
        result = await self.host.tools.invoke(run.id, "apply_patch", {"path": "result.txt", "operation": "add", "content": "Evidence"}, "patch")
        self.assertFalse(result.get("isError", False))
        self.assertIn("apply_patch", self.host.active[run.id].tool_names())
        saved = await self.host.agents.get("worker")
        definition = AgentDefinition.model_validate(saved["definition"])
        await self.host.agents.save(definition.model_copy(update={"manager": definition.manager.model_copy(update={"tools": Tools()})}), expected_revision=saved["revision"])
        with self.assertRaises(RuntimeFault):
            await self.host.tools.invoke(run.id, "apply_patch", {"path": "other.txt", "operation": "add", "content": "Denied"}, "revoked")
