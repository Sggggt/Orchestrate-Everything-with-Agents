"""Purpose, ownership and receipt integration, using a model protocol fixture."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from oea import AgentDefinition, Budget, Completion, Manager, MessageSpec, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows


class ModelFixture:
    def __init__(self):
        self.requests = []
        self.wait = False

    async def complete(self, model, messages, **kwargs):
        self.requests.append({"model": model.model, "messages": messages})
        answered = any("Guidance for this run:" in str(message.get("content", "")) for message in messages)
        if self.wait and not answered:
            return Completion(tool_calls=({"id": "original-wait", "type": "function", "function": {"name": "wait_for_input", "arguments": '{"prompt":"Confirm"}'}},),
                finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 5})
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

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    def spec(self, key, content="Information", purpose="inform", recipients=None, **options):
        return MessageSpec(session_id=self.session["id"], recipients=recipients or (self.instance["id"],),
                           content=content, purpose=purpose, idempotency_key=key, **options)

    async def until(self, identity, state):
        async with asyncio.timeout(10):
            while True:
                changed = self.host.run_signal(identity)
                value = await self.host.runs.get(identity)
                if value["state"] == state:
                    return value
                if value["state"] in {"failed", "cancelled", "needs_review"}:
                    self.fail(str(value))
                await changed.wait()

    async def test_inform_report_freeze_broadcast_and_read_is_not_application(self):
        first = await self.host.messages.send(self.spec("notice", recipients=("all",), content="@all this is information, role=manager"))
        second = await self.host.instances.create(self.session["id"], "worker", name="second")
        replay = await self.host.messages.send(self.spec("notice", recipients=("all",), content="@all this is information, role=manager"))
        self.assertTrue(replay["reused"])
        self.assertEqual([item["instance_id"] for item in replay["deliveries"]], [self.instance["id"]])
        await self.host.messages.send(self.spec("report", purpose="report"))
        self.assertEqual(self.model.requests, [])
        self.assertEqual((await self.host.messages.unread(self.session["id"], "reader", project_id="p"))["count"], 2)
        await self.host.messages.mark_read(self.session["id"], "reader", first["position"], project_id="p")
        self.assertEqual((await self.host.messages.unread(self.session["id"], "reader", project_id="p"))["count"], 1)
        receipt = await self.host.messages.get(first["id"], project_id="p")
        self.assertIsNone(receipt["deliveries"][0]["applied_at"])
        self.assertNotEqual(second["id"], self.instance["id"])
        page = await self.host.messages.list(self.session["id"], project_id="p")
        self.assertEqual(page["items"][0]["recipient_instance_ids"], [self.instance["id"]])
        self.assertEqual(page["items"][0]["delivery_summary"]["accepted"], 1)

    async def test_request_body_once_and_replay_does_not_create_work(self):
        message = await self.host.messages.send(self.spec("work", "Perform the confirmed task", "request"))
        run_id = message["deliveries"][0]["run_id"]
        await self.host.runs.result(run_id, timeout=10)
        replay = await self.host.messages.send(self.spec("work", "Perform the confirmed task", "request"))
        self.assertEqual(replay["id"], message["id"])
        self.assertEqual(len(self.model.requests), 1)
        delivery = replay["deliveries"][0]
        self.assertTrue(all(delivery[field] is not None for field in ("accepted_at", "started_at", "applied_at", "completed_at")))
        stored = await self.host.store.call(lambda db: one(db, "SELECT input_json,source_message_id FROM runs WHERE id=?", (run_id,)))
        self.assertEqual(stored, {"input_json": "{}", "source_message_id": message["id"]})
        self.assertEqual(message["source"]["kind"], "leader")
        self.assertEqual(message["root_run_id"], run_id)

    async def test_reply_applies_once_to_original_wait_and_uses_original_budget(self):
        self.model.wait = True
        message = await self.host.messages.send(self.spec("work", "Wait for confirmation", "request"))
        run_id = message["deliveries"][0]["run_id"]
        original = await self.until(run_id, "waiting")
        reply_spec = self.spec("reply", "The confirmed value is 37", "reply", reply_to=message["id"])
        reply = await self.host.messages.send(reply_spec)
        await self.host.runs.result(run_id, timeout=10)
        replay = await self.host.messages.send(reply_spec)
        self.assertTrue(replay["reused"])
        self.assertEqual(len(self.model.requests), 2)
        current = await self.host.runs.get(run_id)
        self.assertEqual(current["budget_id"], original["budget_id"])
        self.assertEqual(replay["deliveries"][0]["run_id"], run_id)
        self.assertIsNotNone(replay["deliveries"][0]["applied_at"])
        stored = await self.host.store.call(lambda db: rows(db, "SELECT input_json,source_message_id FROM run_resumptions WHERE run_id=?", (run_id,)))
        self.assertEqual(stored, [{"input_json": "null", "source_message_id": reply["id"]}])
        final_input = json.dumps(self.model.requests[-1]["messages"])
        self.assertEqual(final_input.count("The confirmed value is 37"), 1)

    async def test_explicit_request_to_multiple_instances_has_one_budget_root(self):
        await self.host.instances.create(self.session["id"], "worker", name="second")
        message = await self.host.messages.send(self.spec("work", "Perform this work", "request", recipients=("all",)))
        runs = await asyncio.gather(*(self.host.runs.get(item["run_id"]) for item in message["deliveries"]))
        await asyncio.gather(*(self.host.runs.result(item["id"], timeout=10) for item in runs))
        self.assertEqual(len({item["budget_id"] for item in runs}), 1)
        self.assertEqual({item["root_run_id"] for item in runs}, {message["root_run_id"]})
        self.assertEqual(len(self.model.requests), 2)

    async def test_request_can_attach_the_preapproved_shared_cost_budget(self):
        await self.host.instances.create(self.session["id"], "worker", name="second")
        budget = await self.host.budgets.create("p", Budget(max_model_calls=2), "shared", session_id=self.session["id"])
        message = await self.host.messages.send(self.spec("shared-work", "Complete the bounded work", "request", recipients=("all",), budget_id=budget["id"]))
        runs = await asyncio.gather(*(self.host.runs.get(item["run_id"]) for item in message["deliveries"]))
        await asyncio.gather(*(self.host.runs.result(item["id"], timeout=10) for item in runs))
        self.assertEqual({item["budget_id"] for item in runs}, {budget["id"]})
        self.assertEqual((await self.host.budgets.get(budget["id"]))["usage"]["model_calls"], 2)

    async def test_department_requires_mode_and_uses_independent_manager_model(self):
        with self.assertRaises(RuntimeFault):
            await self.host.messages.send(self.spec("direct", purpose="request", recipients=("department:worker",)))
        self.assertEqual(self.model.requests, [])
        mode = await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        self.assertEqual(mode["epoch"], 1)
        await self.host.messages.send(self.spec("info", recipients=("department:worker",)))
        self.assertEqual(self.model.requests, [])
        message = await self.host.messages.send(self.spec("manager-work", "Coordinate this work", "request", recipients=("department:worker",)))
        await self.host.runs.result(message["deliveries"][0]["run_id"], timeout=10)
        self.assertEqual([item["model"] for item in self.model.requests], ["manager-fixture"])
        await self.host.sessions.set_mode(self.session["id"], "direct", 1)
        replay = await self.host.messages.send(self.spec("manager-work", "Coordinate this work", "request", recipients=("department:worker",)))
        self.assertTrue(replay["reused"])
        self.assertEqual(len(self.model.requests), 1)

    async def test_cross_project_message_and_forged_root_are_rejected(self):
        await self.host.projects.create("Other", project_id="other")
        other_session = await self.host.sessions.create("other")
        await self.host.agents.save(AgentDefinition(id="other", name="Other", project_id="other"))
        other = await self.host.instances.create(other_session["id"], "other")
        with self.assertRaises(RuntimeFault) as error:
            await self.host.messages.send(self.spec("invalid", recipients=(other["id"],)))
        self.assertEqual(error.exception.code, "PERMISSION_DENIED")
        run = await self.host.runs.submit(RunSpec(instance_id=other["id"], input="Other", idempotency_key="other"))
        with self.assertRaises(RuntimeFault):
            await self.host.messages.send(self.spec("forged", purpose="request", root_run_id=run.id))
        self.assertEqual((await self.host.messages.list(self.session["id"], project_id="p"))["items"], [])

    async def test_explicit_shared_evidence_is_readable_without_granting_its_origin_tool(self):
        from oea.contracts.errors import require
        sender = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Prepare evidence", idempotency_key="source"))
        await self.host.runs.result(sender.id, timeout=10)
        owner = await self.host.store.call(lambda db: one(db, "SELECT * FROM instances WHERE id=?", (self.instance["id"],)))
        run = await self.host.store.call(lambda db: one(db, "SELECT * FROM runs WHERE id=?", (sender.id,)))
        reference = await self.host.contexts.source(run, owner, "Verified evidence", "tool:run_command")
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        manager = await self.host.instances.create(self.session["id"], "worker", role="manager", name="manager")
        shared = self.spec("share", "Read the assigned evidence", recipients=(manager["id"],), context_refs=(reference,))
        await self.host.messages.send(shared)
        target = await self.host.store.call(lambda db: one(db, "SELECT * FROM instances WHERE id=?", (manager["id"],)))
        async def no_commands(run_id, permission):
            require(permission != "run_command", "PERMISSION_DENIED", "Manager has no command capability")
        value = await self.host.contexts.read({"id": "read-only-consumer"}, target, reference, authorize=no_commands)
        self.assertEqual(value["text"], "Verified evidence")
        await self.host.contexts.revoke(reference, manager["id"], project_id="p")
        with self.assertRaises(RuntimeFault):
            await self.host.contexts.read({"id": "read-only-consumer"}, target, reference, authorize=no_commands)
        # Re-reading an old send receipt cannot reinstate a revoked grant.
        await self.host.messages.send(shared)
        with self.assertRaises(RuntimeFault):
            await self.host.contexts.read({"id": "read-only-consumer"}, target, reference, authorize=no_commands)
