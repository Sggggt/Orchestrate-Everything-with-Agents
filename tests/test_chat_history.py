"""Native checkpoint tails, legacy loading and storage growth."""

import tempfile
import asyncio
import unittest
from pathlib import Path

from ma_runtime import AgentConfig, Runtime, RuntimeFault
from ma_runtime.models import json_dump
from ma_runtime.store import one, rows
from test_skill_system import PolicyLLM


class ChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-chat-history-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root).__aenter__()
        self.config = AgentConfig(id="worker", name="Worker", kind="llm", base_url="http://model.invalid/v1", model="test")
        await self.rt.agents.configure(self.config)
        self.adapter = PolicyLLM(self.config)
        self.rt.adapters["worker"] = self.adapter
        self.session = await self.rt.sessions.create()
        self.thread = await self.rt.threads.create(self.session.id, "worker")

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def turn(self, text, key):
        task = await self.rt.threads.turn(self.thread.id, text, key)
        await self.rt.tasks.result(task.id, timeout=5)
        return await self.rt.tasks.get(task.id)

    async def test_checkpoint_storage_is_linear_and_resume_retains_all_messages(self):
        source_bytes = 0
        for index in range(32):
            text = f"Section {index}: " + "source evidence " * 60
            source_bytes += len(text.encode("utf-8"))
            await self.turn(text, str(index))
        values = await self.rt.store.call(lambda db: {
            "checkpoint": one(db, "SELECT SUM(length(CAST(ref_json AS BLOB))) AS size FROM checkpoints").size,
            "segments": one(db, "SELECT SUM(length(CAST(messages_json AS BLOB))) AS size FROM chat_segments").size,
            "rows": rows(db, "SELECT ref_json FROM checkpoints"),
        })
        self.assertLess(values["checkpoint"] + values["segments"], source_bytes * 2)
        self.assertTrue(all("messages" not in row.ref_json for row in values["rows"]))
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root).__aenter__()
        self.adapter = PolicyLLM(self.config)
        self.rt.adapters["worker"] = self.adapter
        await self.turn("Continue the review", "after-restart")
        history = self.adapter.captured[0]["messages"]
        self.assertEqual(len([message for message in history if message["role"] == "user"]), 33)
        self.assertIn("Section 0:", history[1]["content"])

    async def test_legacy_chat_checkpoint_is_loaded_and_extended_without_rewriting_it(self):
        task = await self.turn("Legacy evidence", "legacy")
        checkpoint_id = task.runs[-1].boundary_receipt["checkpoint_id"]
        legacy = {"task_id": task.id, "messages": [*self.adapter.captured[0]["messages"], {"role": "assistant", "content": "Finished"}]}
        def change(db):
            db.execute("DELETE FROM chat_checkpoint_heads WHERE checkpoint_id=?", (checkpoint_id,))
            db.execute("UPDATE checkpoints SET format_version='chat-messages-v1',ref_json=? WHERE id=?", (json_dump(legacy), checkpoint_id))
        await self.rt.store.call(change, transaction=True)
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root).__aenter__()
        self.adapter = PolicyLLM(self.config)
        self.rt.adapters["worker"] = self.adapter
        continued = await self.turn("Continue", "new")
        self.assertIn("Legacy evidence", json_dump(self.adapter.captured[0]["messages"]))
        old = await self.rt.store.call(lambda db: one(db, "SELECT format_version,ref_json FROM checkpoints WHERE id=?", (checkpoint_id,)))
        self.assertEqual(old.format_version, "chat-messages-v1")
        self.assertEqual(old.ref_json, json_dump(legacy))
        new_id = continued.runs[-1].boundary_receipt["checkpoint_id"]
        new = await self.rt.store.call(lambda db: one(db, "SELECT format_version FROM checkpoints WHERE id=?", (new_id,)))
        self.assertEqual(new.format_version, "chat-messages-v2")

    async def test_bounded_native_projection_keeps_complete_history_and_shared_branch_cache(self):
        for index in range(20):
            task = await self.rt.threads.turn(self.thread.id, f"Evidence {index}: " + "source " * 800,
                                             str(index), context_budget_tokens=4096)
            await self.rt.tasks.result(task.id, timeout=5)
        self.assertLess(len(self.adapter.captured[-1]["messages"]), 8)
        self.assertTrue(any(tool["function"]["name"] == "read_history" for tool in self.adapter.captured[-1]["tools"]))
        point = (await self.rt.threads.get(self.thread.id)).latest_checkpoint_id
        cursor, all_messages = None, []
        while True:
            page = await self.rt.threads.history(self.thread.id, checkpoint_id=point, cursor=cursor, limit=7)
            all_messages.extend(page["messages"])
            cursor = page["cursor"]
            if cursor is None:
                break
        self.assertEqual(page["message_count"], 40)
        self.assertEqual(len(all_messages), 40)
        self.assertIn("Evidence 0:", all_messages[0]["content"])
        self.assertIn("Evidence 19:", all_messages[-2]["content"])
        branch = await self.rt.threads.fork(self.thread.id, idempotency_key="bounded-branch")
        def load(db):
            origin = one(db, "SELECT * FROM threads WHERE id=?", (self.thread.id,))
            fork = one(db, "SELECT * FROM threads WHERE id=?", (branch.id,))
            source = one(db, "SELECT * FROM checkpoints WHERE id=?", (point,))
            target = one(db, "SELECT * FROM checkpoints WHERE id=?", (fork.latest_checkpoint_id,))
            a = self.rt.chat_history.load(db, source, origin, max_bytes=8192)
            b = self.rt.chat_history.load(db, target, fork, max_bytes=8192)
            return a, b
        original, inherited = await self.rt.store.call(load)
        self.assertIs(original["messages"], inherited["messages"])
        self.assertEqual(original["message_count"], 40)
        self.assertLess(original["loaded_message_count"], 8)
        with self.assertRaises(RuntimeFault) as fault:
            await self.rt.threads.history(branch.id, checkpoint_id=point)
        self.assertEqual(fault.exception.code, "PERMISSION_DENIED")

    async def test_forks_share_prefix_and_paginate_without_copying_rows(self):
        first = await self.turn("First evidence", "first")
        point = first.runs[-1].boundary_receipt["checkpoint_id"]
        await self.turn("Later source-only evidence", "later")
        before = await self.rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM conversation").count)
        forks = await asyncio.gather(*(self.rt.threads.fork(self.thread.id, checkpoint_id=point, idempotency_key="fork") for _ in range(3)))
        self.assertEqual(len({branch.id for branch in forks}), 1)
        branch = forks[0]
        after = await self.rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM conversation").count)
        self.assertEqual(before, after)
        inherited = await self.rt.threads.conversation(branch.id)
        self.assertEqual([entry.content for entry in inherited.items], ["First evidence", "Finished"])
        latest = await self.rt.threads.conversation(branch.id, latest=True, limit=1)
        older = await self.rt.threads.conversation(branch.id, latest=True, before_seq=latest.cursor, limit=1)
        self.assertTrue(latest.has_more)
        self.assertEqual(older.items[0].content, "First evidence")
        task = await self.rt.threads.turn(branch.id, "Branch-only evidence", "branch-turn")
        await self.rt.tasks.result(task.id, timeout=5)
        self.assertNotIn("Later source-only evidence", json_dump(self.adapter.captured[-1]["messages"]))
        nested = await self.rt.threads.fork(branch.id, idempotency_key="nested")
        self.assertEqual([entry.content for entry in (await self.rt.threads.conversation(nested.id)).items],
                         ["First evidence", "Finished", "Branch-only evidence", "Finished"])
        with self.assertRaises(RuntimeFault) as fault:
            await self.rt.threads.fork(self.thread.id, title="Changed", checkpoint_id=point, idempotency_key="fork")
        self.assertEqual(fault.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_fork_window_excludes_input_queued_before_the_selected_reply(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.adapter._completion
        async def held(messages, context, tools=None):
            if context.request.payload["input"] == "First queued goal":
                started.set()
                await release.wait()
            async for event in original(messages, context, tools):
                yield event
        self.adapter._completion = held
        first = await self.rt.threads.turn(self.thread.id, "First queued goal", "first-queued")
        await asyncio.wait_for(started.wait(), 5)
        second = await self.rt.threads.turn(self.thread.id, "Not consumed at that point", "second-queued")
        release.set()
        await self.rt.tasks.result(first.id, timeout=5)
        await self.rt.tasks.result(second.id, timeout=5)
        entry = next(message for message in (await self.rt.threads.conversation(self.thread.id)).items if message.task_id == first.id and message.role == "assistant")
        branch = await self.rt.threads.fork(self.thread.id, message_id=entry.id, idempotency_key="queued-point")
        self.assertEqual([message.content for message in (await self.rt.threads.conversation(branch.id)).items], ["First queued goal", "Finished"])

    async def test_fork_does_not_inherit_evidence_from_a_later_run_of_the_same_task(self):
        references = []
        async def evidence(messages, context, tools=None):
            reference = await self.rt.contexts.register(context, f"Evidence from run {len(references)}", "history:" + self.thread.id)
            references.append(reference)
            yield {"calls": [], "text": "Partial. " if not context.request.continuation else "Complete.",
                   "finish_reason": "stop" if context.request.continuation else "length"}
        self.adapter._completion = evidence
        first = await self.rt.threads.turn(self.thread.id, "Inspect source evidence", "partial")
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(first.id, timeout=5)
        point = (await self.rt.tasks.get(first.id)).runs[-1].boundary_receipt["checkpoint_id"]
        await self.rt.tasks.continue_result(first.id, "continue")
        await self.rt.tasks.result(first.id, timeout=5)
        branch = await self.rt.threads.fork(self.thread.id, checkpoint_id=point, idempotency_key="old-window")
        reads = []
        async def check(messages, context, tools=None):
            reads.append(await context.read_context(references[0]))
            try:
                await context.read_context(references[1])
            except RuntimeFault as exc:
                reads.append(exc.code)
            yield {"calls": [], "text": "Checked inherited evidence", "finish_reason": "stop"}
        self.adapter._completion = check
        task = await self.rt.threads.turn(branch.id, "Check inherited sources", "check")
        await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(reads[0]["text"], "Evidence from run 0")
        self.assertEqual(reads[1], "PERMISSION_DENIED")

    async def test_continuation_reuses_external_receipt_and_blocks_a_new_action(self):
        from ma_runtime.tools import ToolSpec
        invoked, denied = [], []
        class ExternalTool:
            def describe(self):
                return ToolSpec(name="external_action", side_effects="EXTERNAL", supports_idempotency=True)
            async def invoke(self, request):
                invoked.append(request)
                return {"saved": True}
        self.rt.tools.register(ExternalTool(), allowed_agent_ids=["worker"])
        async def complete(messages, context, tools=None):
            value = await context.invoke_tool("external_action", {}, request_key := context.request.task_id + ":external")
            self.assertEqual(value, {"saved": True})
            if context.request.continuation:
                with self.assertRaises(RuntimeFault) as fault:
                    await context.invoke_tool("external_action", {}, request_key + ":new")
                denied.append(fault.exception.code)
                yield {"calls": [], "text": "Completed delivery", "finish_reason": "stop"}
            else:
                yield {"calls": [], "text": "Retained output. ", "finish_reason": "length"}
        self.adapter._completion = complete
        task = await self.rt.threads.turn(self.thread.id, "Deliver the result", "external")
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=5)
        await self.rt.tasks.continue_result(task.id, "finish")
        await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(len(invoked), 1)
        self.assertEqual(denied, ["CONTINUATION_ACTION_RESTRICTED"])
        self.assertEqual(len((await self.rt.workbench.calls(task.id))["items"]), 1)

    async def test_continuation_deadline_is_durable_and_replay_does_not_extend_it(self):
        async def incomplete(messages, context, tools=None):
            yield {"calls": [], "text": "Partial output. ", "finish_reason": "length"}
        self.adapter._completion = incomplete
        task = await self.rt.threads.turn(self.thread.id, "Review", "deadline")
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=5)
        await self.rt.tasks.continue_result(task.id, "first")
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=5)
        await self.rt.store.call(lambda db: db.execute("UPDATE task_continuations SET deadline_at=0 WHERE task_id=?", (task.id,)), transaction=True)
        self.assertTrue((await self.rt.tasks.continue_result(task.id, "first")).reused)
        with self.assertRaises(RuntimeFault) as expired:
            await self.rt.tasks.continue_result(task.id, "second")
        self.assertEqual(expired.exception.code, "CONTINUATION_BUDGET_EXCEEDED")
        self.assertEqual(len((await self.rt.tasks.get(task.id)).runs), 2)
