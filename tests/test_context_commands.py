"""Compaction retains current command facts without replaying historical work."""

import asyncio
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_context as fixture
from oea.context.store import ContextStore, message_groups
from oea.contracts.configuration import ContextPolicy
from oea.contracts.errors import RuntimeFault
from oea.storage.persistence import encode, one, rows
from oea.workspace.commands import CommandSessions


class CommandContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture.ContextTests("test_followup_input_follows_previous_work_and_precedes_current_tool_continuation")
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.commands = CommandSessions(Path(self.fixture.temporary.name) / "commands")
        self.reads = []
        def reader(commands):
            self.reads.append(dict(commands))
            return self.commands.facts(commands)
        self.reader = reader
        self.contexts = ContextStore(self.fixture.store, command_facts=reader)
        self.definition = self.fixture.high_input_definition()

    async def action(self, identity, name, value, *, run_id="r"):
        await self.fixture.store.call(lambda db: db.execute(
            "INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,created_at,updated_at) "
            "VALUES(?,?,?,?, 'saved','{}','local','completed',?,1,1)",
            (identity, run_id, identity, name, encode(value))), transaction=True)

    async def command(self, *, state="exited", stopped=True, exit_code=0, termination=None, identity="command", run_id="r"):
        original = {"command_session_id": identity, "state": "running", "process_stopped": False}
        await self.action(identity, "exec_command", original, run_id=run_id)
        record = {"command_session_id": identity, "action_id": identity, "run_id": run_id,
                  "generation": self.commands.generation, "state": state, "process_stopped": stopped,
                  "exit_code": exit_code, "termination": termination, "started_at": 1,
                  "stopped_at": 2 if stopped else None}
        self.commands.directory(identity).mkdir(parents=True)
        self.commands.save(record)
        if state == "running":
            self.commands.live[identity] = {"record": record}
        return original, record

    async def observation(self, value, *, name="exec_command", key="original", action_key=None):
        call = {"id": key, "type": "function", "function": {"name": name, "arguments": "{}"}}
        await self.contexts.append(self.fixture.run, self.fixture.instance,
                                   {"role": "assistant", "tool_calls": [call]}, key + ":call")
        await self.contexts.tool_result(self.fixture.run, self.fixture.instance, self.definition.context,
                                       call, value, action_key=action_key, identity=key + ":result")

    async def filler(self):
        await self.fixture.append_messages({"role": "user", "content": "Evidence " + str(i)} for i in range(300))

    async def project(self, *, contexts=None, summarize=None):
        async def summary(messages, limit):
            return "Confirmed facts and original source references remain available."
        return await (contexts or self.contexts).project(self.fixture.run, self.fixture.instance,
            self.definition, "Review", [], summarize=summarize or summary)

    def cards(self, view):
        return [card for message in view if message.get("content", "").startswith("Command lifecycle snapshots")
                for card in json.loads(message["content"].split("\n", 1)[1])]

    async def test_later_terminal_fact_unblocks_prefix_and_preserves_original_receipts(self):
        original, _ = await self.command()
        await self.observation(original, action_key="command")
        await self.filler()
        await self.observation({**original, "state": "exited", "process_stopped": True, "exit_code": 0},
                               name="write_stdin", key="later")
        before = await self.fixture.store.call(lambda db: rows(db, "SELECT * FROM conversation ORDER BY position"))
        payloads = []
        async def summary(messages, limit):
            payloads.append(json.loads(messages[1]["content"]))
            return "The original command exited successfully; continue from retained evidence."
        await self.project(summarize=summary)
        self.assertEqual(payloads[0]["command_facts"][0]["state"], "exited")
        self.assertEqual(payloads[0]["command_facts"][0]["exit_code"], 0)
        after = await self.fixture.store.call(lambda db: rows(db, "SELECT * FROM conversation ORDER BY position"))
        self.assertEqual(before, after)
        saved = await self.fixture.store.call(lambda db: one(db, "SELECT result_json FROM actions WHERE id='command'"))
        self.assertEqual(json.loads(saved["result_json"]), original)
        self.assertEqual(self.reads, [{"command": "r"}])

    async def test_session_metadata_alone_resolves_many_running_observations_once(self):
        original, _ = await self.command()
        for index in range(40):
            await self.observation(original, name="write_stdin", key="poll-" + str(index))
        view, _ = await self.project()
        self.assertEqual(self.reads, [{"command": "r"}])
        self.assertEqual(len(self.cards(view)), 1)
        self.assertTrue(self.cards(view)[0]["process_stopped"])

    async def test_active_identity_survives_summary_and_refreshes_after_reopening(self):
        original, record = await self.command(state="running", stopped=False, exit_code=None)
        await self.observation(original)
        await self.filler()
        view, _ = await self.project()
        self.assertEqual(self.cards(view)[0]["state"], "running")
        memory = await self.fixture.store.call(lambda db: one(db, "SELECT * FROM memories ORDER BY rowid DESC LIMIT 1"))
        self.assertIn("active-command:command", json.loads(memory["source_ids_json"]))
        self.assertNotIn("command", memory["body"])
        record.update(state="exited", process_stopped=True, exit_code=0, stopped_at=3)
        self.commands.save(record)
        reopened = CommandSessions(self.commands.root)
        view, _ = await self.project(contexts=ContextStore(self.fixture.store, command_facts=reopened.facts))
        self.assertEqual(self.cards(view)[0]["state"], "exited")
        self.assertEqual(self.cards(view)[0]["exit_code"], 0)

    async def test_lost_ownership_retains_unknown_without_inventing_terminal_state(self):
        original, _ = await self.command(state="running", stopped=False, exit_code=None)
        self.commands.live.clear()
        await self.observation(original)
        await self.filler()
        view, _ = await self.project()
        card = self.cards(view)[0]
        self.assertEqual(card["state"], "unknown")
        self.assertFalse(card["process_stopped"])
        self.assertIsNone(card["exit_code"])
        saved = json.loads((self.commands.directory("command") / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "running")

    async def test_missing_metadata_preserves_a_summarized_command_as_unknown(self):
        original, _ = await self.command(state="running", stopped=False, exit_code=None)
        await self.observation(original)
        await self.filler()
        await self.project()
        self.commands.live.clear()
        (self.commands.directory("command") / "session.json").unlink()
        view, _ = await self.project()
        card = self.cards(view)[0]
        self.assertEqual(card["state"], "unknown")
        self.assertFalse(card["process_stopped"])
        self.assertEqual(card["error"]["code"], "COMMAND_NOT_FOUND")

    async def test_restoring_active_references_requires_the_command_reader(self):
        original, _ = await self.command(state="running", stopped=False, exit_code=None)
        await self.observation(original)
        await self.filler()
        await self.project()
        with self.assertRaises(RuntimeFault) as unavailable:
            await self.project(contexts=ContextStore(self.fixture.store))
        self.assertEqual(unavailable.exception.code, "CONTEXT_RECOVERY_REQUIRED")

    async def test_business_running_field_does_not_become_platform_liveness(self):
        value = {"state": "running", "description": "Business metadata"}
        await self.action("domain", "mcp_business_status", value)
        await self.observation(value, name="mcp_business_status", action_key="domain")
        await self.filler()
        view, _ = await self.project()
        self.assertFalse(self.reads)
        self.assertFalse(self.cards(view))

    async def test_explicit_unresolved_effects_remain_protected(self):
        value = {"state": "running", "unresolved_actions": ["pending-effect"]}
        await self.action("domain", "mcp_business_status", value)
        await self.observation(value, name="mcp_business_status", action_key="domain")
        await self.filler()
        with self.assertRaises(RuntimeFault) as blocked:
            await self.project()
        self.assertEqual(blocked.exception.code, "MODEL_CONTEXT_BUDGET")
        self.assertFalse(self.reads)

    async def test_foreign_command_identity_never_reaches_the_metadata_reader(self):
        original, _ = await self.command(identity="foreign", run_id="other")
        await self.observation(original)
        await self.filler()
        with self.assertRaises(RuntimeFault) as blocked:
            await self.project()
        self.assertEqual(blocked.exception.code, "MODEL_CONTEXT_BUDGET")
        self.assertFalse(self.reads)

    async def test_summary_failure_recovery_uses_the_same_terminal_facts(self):
        original, _ = await self.command()
        await self.observation(original)
        await self.filler()
        async def fail(messages, limit):
            raise RuntimeFault("MODEL_FAILED", "Synthetic summary failure")
        view, projection = await self.project(summarize=fail)
        self.assertEqual(projection["selection"]["fallback"], "persistent_activity")
        self.assertEqual(self.cards(view)[0]["state"], "exited")
        self.assertEqual(await self.fixture.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM memories")["n"]), 0)

    async def test_nonzero_exit_and_cancellation_keep_terminal_details(self):
        original, record = await self.command(exit_code=7)
        await self.observation({**original, "content_view": {"integrity": "truncated", "source_complete": False}})
        view, _ = await self.project()
        self.assertEqual(self.cards(view)[0]["exit_code"], 7)
        self.assertTrue(self.cards(view)[0]["process_stopped"])
        record.update(exit_code=None, termination="cancelled")
        self.commands.save(record)
        view, _ = await self.project()
        self.assertIsNone(self.cards(view)[0]["exit_code"])
        self.assertEqual(self.cards(view)[0]["termination"], "cancelled")

    async def test_active_reference_survives_multiple_compaction_windows(self):
        original, _ = await self.command(state="running", stopped=False, exit_code=None)
        await self.observation(original)
        await self.filler()
        await self.project()
        await self.fixture.append_messages(({"role": "user", "content": "Later evidence " + str(i)} for i in range(300)), prefix="later")
        view, _ = await self.project()
        self.assertEqual(self.cards(view)[0]["state"], "running")
        self.assertEqual(self.reads, [{"command": "r"}, {"command": "r"}])

    async def test_settlement_during_summary_keeps_a_reference_for_the_next_projection(self):
        original, record = await self.command(state="running", stopped=False, exit_code=None)
        await self.observation(original)
        await self.filler()
        async def finish(messages, limit):
            self.assertEqual(json.loads(messages[1]["content"])["command_facts"][0]["state"], "running")
            self.commands.live.clear()
            record.update(state="exited", process_stopped=True, exit_code=0, stopped_at=3)
            self.commands.save(record)
            return "Work was active at the captured boundary; consult current command facts."
        await self.project(summarize=finish)
        view, _ = await self.project()
        self.assertEqual(self.cards(view)[0]["state"], "exited")
        self.assertTrue(self.cards(view)[0]["process_stopped"])

    async def test_metadata_read_does_not_wait_or_read_command_output(self):
        await self.command()
        with patch.object(self.commands, "observe", side_effect=AssertionError("Must not observe output")), \
             patch.object(self.commands, "result", side_effect=AssertionError("Must not consume output")):
            snapshots = self.commands.facts({"command": "r"})
        self.assertEqual(snapshots["command"]["state"], "exited")
        self.assertNotIn("stdout", snapshots["command"])
        self.assertNotIn("environment", snapshots["command"])

    async def test_fact_view_preserves_the_prefix_and_latest_tool_pair(self):
        original, _ = await self.command()
        await self.fixture.append_messages(({"role": "user", "content": "Stable earlier evidence"},))
        await self.observation(original)
        latest = [{"role": "assistant", "reasoning_content": "provider continuation", "tool_calls": [
                      {"id": "last", "type": "function", "function": {"name": "write_stdin", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "last", "content": encode(original)}]
        await self.fixture.append_messages(latest, prefix="latest")
        baseline, _ = await self.project(contexts=ContextStore(self.fixture.store))
        view, _ = await self.project()
        position = next(index for index, message in enumerate(view) if message.get("content", "").startswith("Command lifecycle snapshots"))
        self.assertEqual(view[:position], baseline[:position])
        self.assertEqual(view[-2:], latest)
        message_groups(view)

    async def test_status_views_are_reserved_before_selecting_history(self):
        original, _ = await self.command(state="running", stopped=False, exit_code=None)
        await self.observation(original)
        await self.fixture.append_messages(({"role": "user", "content": "Earlier evidence " * 2000},), prefix="earlier")
        await self.fixture.append_messages(({"role": "user", "content": "Latest"},), prefix="latest")
        self.definition = self.definition.model_copy(update={"context": ContextPolicy(input_tokens=4096, memory_tokens=512, pressure=.999999999)})
        async def unexpected(messages, limit):
            raise AssertionError("Reference views fit without a summary")
        for length in (10800, 11000, 11200, 11400, 11600, 11800):
            with self.subTest(length=length):
                await self.fixture.store.call(lambda db: db.execute("UPDATE conversation SET body_json=? WHERE id='latest-0'",
                    (encode({"role": "user", "content": "L" * length}),)), transaction=True)
                view, projection = await self.project(summarize=unexpected)
                self.assertLessEqual(projection["input_tokens"], 4096)
                self.assertEqual(self.cards(view)[0]["state"], "running")

    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
    async def test_real_native_settlement_releases_the_historical_running_receipt(self):
        from oea import Completion
        from oea.runtime.host import Host
        from test_command_recovery_v3 import WaitingCommandModel, start_waiting
        class ResumableCommandModel(WaitingCommandModel):
            async def complete(inner, model, messages, **options):
                if any("Guidance for this run:" in str(message.get("content", "")) for message in messages):
                    inner.calls += 1
                    return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 3})
                return await super().complete(model, messages, **options)
        model = ResumableCommandModel("from pathlib import Path; import time; Path('effects.txt').write_text('one'); time.sleep(2)")
        native = Path(os.environ["OEA_TEST_NATIVE_COMPONENT"])
        with patch("oea.workspace.native.executable", return_value=native):
            async with Host(Path(self.fixture.temporary.name) / "native", allow_commands=True, model_api=model) as host:
                handle, definition = await start_waiting(host)
                async with asyncio.timeout(10):
                    while True:
                        changed = host.run_signal(handle.id)
                        if not host.command_sessions.has_live(handle.id):
                            break
                        await changed.wait()
                run, instance, receipt = await host.store.call(lambda db: (
                    one(db, "SELECT * FROM runs WHERE id=?", (handle.id,)),
                    one(db, "SELECT * FROM instances WHERE id=?", (handle.instance_id,)),
                    one(db, "SELECT id,result_json FROM actions WHERE run_id=? AND name='exec_command'", (handle.id,))))
                self.assertEqual(json.loads(receipt["result_json"])["state"], "running")
                for index in range(300):
                    await host.contexts.append(run, instance, {"role": "user", "content": "Evidence " + str(index)}, "evidence-" + str(index))
                payloads = []
                async def summarize(messages, limit):
                    payloads.append(json.loads(messages[1]["content"]))
                    return "The native command has finished; preserve original evidence and continue the work."
                await host.contexts.project(run, instance, definition, "Review", [], summarize=summarize)
                card = payloads[0]["command_facts"][0]
                self.assertEqual((card["state"], card["exit_code"], card["process_stopped"]), ("exited", 0, True))
                current = await host.store.call(lambda db: one(db, "SELECT result_json FROM actions WHERE id=?", (receipt["id"],)))
                self.assertEqual(current["result_json"], receipt["result_json"])
                work = Path(json.loads(instance["workspace_json"])["work_dir"])
                self.assertEqual((work / "effects.txt").read_text(), "one")
                self.assertEqual(model.calls, 1)
                resumed = await host.runs.resume(handle.id, "continue", input="Continue from original evidence")
                self.assertEqual(resumed["id"], handle.id)
                self.assertEqual((await host.runs.result(handle.id, timeout=15)).text, "Complete")
                after = await host.store.call(lambda db: one(db,
                    "SELECT budget_id,(SELECT COUNT(*) FROM actions WHERE run_id=? AND name='exec_command') AS commands FROM runs WHERE id=?",
                    (handle.id, handle.id)))
                self.assertEqual(after, {"budget_id": handle.budget_id, "commands": 1})
                self.assertEqual((work / "effects.txt").read_text(), "one")
