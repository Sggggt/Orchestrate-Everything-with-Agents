"""Concurrent domain transactions, rollback, WAL snapshots, and cost ledgers."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apsw

from oea import AgentDefinition, Budget, Completion, MessageSpec, Model, RunSpec, Tools, ToolSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.runtime.context import RunContext


class BarrierModel:
    """A model protocol fixture held at the real budget/dispatch boundary."""
    def __init__(self, target=1):
        self.target, self.calls, self.active, self.peak = target, 0, 0, 0
        self.ready, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, model, messages, **options):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == self.target:
            self.ready.set()
        try:
            await self.release.wait()
            return Completion(text="Complete", usage={"prompt_tokens": 20, "completion_tokens": 3})
        finally:
            self.active -= 1


class InputModel:
    async def complete(self, model, messages, **options):
        replied = any("Guidance for this run:" in str(message.get("content")) for message in messages)
        return Completion(text="Complete" if replied else "", finish_reason="stop" if replied else "tool_calls",
            tool_calls=() if replied else ({"id": "wait", "type": "function", "function": {"name": "wait_for_input", "arguments": '{"prompt":"Confirm"}'}},),
            usage={"prompt_tokens": 20, "completion_tokens": 3})


class ReceiptModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        observed = any(message["role"] == "tool" for message in messages)
        return Completion(text="Complete" if observed else "", finish_reason="stop" if observed else "tool_calls",
            tool_calls=() if observed else ({"id": "commit-once", "type": "function", "function": {"name": "commit", "arguments": "{}"}},),
            usage={"prompt_tokens": 20, "completion_tokens": 3})


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-transactions-")
        self.root = Path(self.temporary.name)
        self.model = BarrierModel()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.session = await self.host.sessions.create("p")
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid/v1", model="protocol-fixture", max_output_tokens=32))
        await self.host.agents.save(self.definition, expected_revision=0)

    async def asyncTearDown(self):
        self.model.release.set()
        await self.host.aclose()
        self.temporary.cleanup()

    async def simultaneous(self, operations):
        gate = asyncio.Event()
        async def apply(operation):
            await gate.wait()
            return await operation()
        tasks = [asyncio.create_task(apply(operation)) for operation in operations]
        gate.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def test_thirty_two_identical_submissions_commit_one_run_budget_and_dispatch(self):
        instance = await self.host.instances.create(self.session["id"], "worker")
        spec = RunSpec(instance_id=instance["id"], input="One intended work", idempotency_key="one-intent")
        accepted = await self.simultaneous([lambda: self.host.runs.submit(spec) for _ in range(32)])
        self.assertFalse(any(isinstance(value, Exception) for value in accepted))
        self.assertEqual(len({value.id for value in accepted}), 1)
        self.assertEqual(sum(not value.reused for value in accepted), 1)
        await asyncio.wait_for(self.model.ready.wait(), 5)
        counts = await self.host.store.call(lambda db: {table: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"] for table in ("runs", "budgets", "usage", "actions")})
        self.assertEqual(counts, {"runs": 1, "budgets": 1, "usage": 1, "actions": 1})
        self.model.release.set()
        await self.host.runs.result(accepted[0].id, timeout=5)
        await asyncio.gather(*tuple(self.host.jobs.values()))
        self.assertEqual(self.model.calls, 1)
        facts = await self.host.store.call(lambda db: rows(db, "SELECT kind,COUNT(*) AS count FROM events WHERE kind IN ('run.accepted','model.started','run.completed') GROUP BY kind"))
        self.assertEqual({row["kind"]: row["count"] for row in facts}, {"run.accepted": 1, "model.started": 1, "run.completed": 1})

    async def test_compare_and_swap_rejects_concurrent_stale_configuration_writers(self):
        operations = [lambda index=index: self.host.agents.save(self.definition.model_copy(update={"description": "Revision " + str(index)}), expected_revision=1) for index in range(32)]
        results = await self.simultaneous(operations)
        successful = [value for value in results if not isinstance(value, Exception)]
        conflicts = [value for value in results if isinstance(value, RuntimeFault)]
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(conflicts), 31)
        self.assertTrue(all(error.code == "REVISION_CONFLICT" for error in conflicts))
        saved = await self.host.agents.get("worker")
        self.assertEqual(saved, successful[0])
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.host.definitions["worker"].model_dump(mode="json"), saved["definition"])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events WHERE kind='agent.saved'")["count"]), 2)

    async def test_parallel_model_reservations_cannot_overspend_the_shared_root_budget(self):
        self.model.target = 7
        budget = await self.host.budgets.create("p", Budget(max_model_calls=7, max_input_tokens=100000, max_output_tokens=10000), "shared", session_id=self.session["id"])
        instances = await asyncio.gather(*(self.host.instances.create(self.session["id"], "worker", name="Worker " + str(index)) for index in range(32)))
        accepted = await self.simultaneous([lambda instance=instance: self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Shared work", idempotency_key="work", budget_id=budget["id"])) for instance in instances])
        self.assertFalse(any(isinstance(value, Exception) for value in accepted))
        await asyncio.wait_for(self.model.ready.wait(), 10)
        ledger = await self.host.budgets.get(budget["id"])
        self.assertEqual(ledger["usage"]["model_calls"], 7)
        self.assertEqual(ledger["reserved"]["output_tokens"], 7 * 32)
        self.assertEqual(self.model.peak, 7)
        self.model.release.set()
        outcomes = await asyncio.gather(*(self.host.runs.result(value.id, timeout=10) for value in accepted), return_exceptions=True)
        self.assertEqual(sum(not isinstance(value, Exception) for value in outcomes), 7)
        failures = [value for value in outcomes if isinstance(value, Exception)]
        self.assertEqual(len(failures), 25)
        self.assertTrue(all(isinstance(value, RuntimeFault) and value.code == "BUDGET_EXHAUSTED" for value in failures))
        ledger = await self.host.budgets.get(budget["id"])
        self.assertEqual(ledger["reserved"], {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(ledger["provider_reported"]["input_tokens"], 140)
        self.assertEqual(ledger["provider_reported"]["output_tokens"], 21)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM actions WHERE name='model.complete'")["count"]), 7)

    async def test_concurrent_message_fanout_rolls_back_body_delivery_runs_budget_and_events(self):
        first, second = await asyncio.gather(*(self.host.instances.create(self.session["id"], "worker", name=name) for name in ("First", "Second")))
        original = self.host.runs._submit
        def injected(db, spec, **options):
            message = one(db, "SELECT body FROM messages WHERE id=?", (options.get("source_message_id"),))
            if spec.instance_id == second["id"] and message["body"] == "fault":
                raise RuntimeFault("FAULT_INJECTED", "Second child acceptance failed inside the transaction")
            return original(db, spec, **options)
        with patch.object(self.host.runs, "_submit", side_effect=injected):
            results = await self.simultaneous([lambda index=index: self.host.messages.send(MessageSpec(session_id=self.session["id"], recipients=(first["id"], second["id"]),
                content="fault" if index % 2 == 0 else "valid", purpose="request", idempotency_key="message-" + str(index))) for index in range(16)])
        self.assertEqual(sum(isinstance(value, RuntimeFault) and value.code == "FAULT_INJECTED" for value in results), 8)
        self.assertEqual(sum(not isinstance(value, Exception) for value in results), 8)
        counts = await self.host.store.call(lambda db: {table: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"] for table in ("messages", "deliveries", "runs", "budgets")})
        self.assertEqual(counts, {"messages": 8, "deliveries": 16, "runs": 16, "budgets": 8})
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM messages WHERE body='fault'")["count"]), 0)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events WHERE kind='run.accepted'")["count"]), 16)
        self.model.release.set()
        run_ids = await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs"))
        await asyncio.gather(*(self.host.runs.result(value["id"], timeout=10) for value in run_ids))

    async def test_wal_reader_retains_a_consistent_snapshot_while_domain_transactions_commit(self):
        reader = apsw.Connection(str(self.host.root / "oea.sqlite"), flags=apsw.SQLITE_OPEN_READONLY)
        try:
            reader.execute("BEGIN")
            self.assertEqual(next(reader.execute("SELECT COUNT(*) FROM projects"))[0], 1)
            results = await self.simultaneous([lambda index=index: self.host.projects.create("Project " + str(index), project_id="p" + str(index)) for index in range(32)])
            self.assertFalse(any(isinstance(value, Exception) for value in results))
            self.assertEqual(next(reader.execute("SELECT COUNT(*) FROM projects"))[0], 1)
            self.assertEqual(next(reader.execute("SELECT COUNT(*) FROM events WHERE kind='project.created'"))[0], 1)
            reader.execute("ROLLBACK")
            self.assertEqual(next(reader.execute("SELECT COUNT(*) FROM projects"))[0], 33)
            self.assertEqual(next(reader.execute("SELECT COUNT(*) FROM events WHERE kind='project.created'"))[0], 33)
        finally:
            reader.close()

    async def test_cancel_resume_race_has_at_most_one_reply_and_cannot_revive_cancelled_work(self):
        self.host.model_api = InputModel()
        for cancel_first in (False, True):
            with self.subTest(cancel_first=cancel_first):
                instance = await self.host.instances.create(self.session["id"], "worker", name="Race " + str(cancel_first))
                run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Wait", idempotency_key="wait"))
                async with asyncio.timeout(5):
                    while True:
                        changed = self.host.run_signal(run.id)
                        if (await self.host.runs.get(run.id))["state"] == "waiting":
                            break
                        await changed.wait()
                cancel = lambda: self.host.runs.cancel(run.id)
                resume = lambda: self.host.runs.resume(run.id, "one-reply", input="Confirmed")
                operations = [cancel, resume] if cancel_first else [resume, cancel]
                await self.simultaneous(operations * 16)
                await asyncio.gather(*tuple(self.host.jobs.values()))
                final = await self.host.runs.get(run.id)
                self.assertEqual(final["state"], "cancelled")
                self.assertIsNotNone(final["stopped_at"])
                replies = await self.host.store.call(lambda db: rows(db, "SELECT applied_at FROM run_resumptions WHERE run_id=?", (run.id,)))
                self.assertLessEqual(len(replies), 1)
                self.assertNotIn(run.id, self.host.active)
                self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events WHERE run_id=? AND kind='run.cancel_requested'", (run.id,))["count"]), 1)

    async def test_twenty_receipts_survive_checkpoint_gap_failure_and_restart_without_repeating_effects(self):
        effect_file = self.root / "external-effects.txt"
        class ExternalCommit:
            def describe(self):
                return ToolSpec(name="commit", side_effects="external")

            async def invoke(self, request):
                with effect_file.open("a", encoding="utf-8") as output:
                    output.write(request["action_id"] + "\n")
                return {"value": 37}
        provider = ExternalCommit()
        self.host.register_tool(provider)
        before = ReceiptModel()
        self.host.model_api = before
        definition = self.definition.model_copy(update={"tools": Tools(functions=("commit",))})
        await self.host.agents.save(definition, expected_revision=1)
        budget = await self.host.budgets.create("p", Budget(max_model_calls=80, max_input_tokens=1000000, max_output_tokens=10000), "recovery-root", session_id=self.session["id"])
        instances = await asyncio.gather(*(self.host.instances.create(self.session["id"], "worker", name="Recovery " + str(index)) for index in range(20)))
        original_execute = RunContext.execute_tools
        reached, all_reached, observed = [], asyncio.Event(), 0
        async def crash_after_receipt(context, state):
            nonlocal observed
            value = await original_execute(context, state)
            reached.append(context.run["id"])
            observed += 1
            if observed == 20:
                all_reached.set()
            await all_reached.wait()
            raise RuntimeFault("FAULT_AFTER_RECEIPT", "The tool receipt committed before the graph checkpoint")
        with patch.object(RunContext, "execute_tools", crash_after_receipt):
            accepted = await self.simultaneous([lambda instance=instance: self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Commit once", idempotency_key="original", budget_id=budget["id"])) for instance in instances])
            await asyncio.wait_for(all_reached.wait(), 15)
            failures = await asyncio.gather(*(self.host.runs.result(run.id, timeout=10) for run in accepted), return_exceptions=True)
        self.assertTrue(all(isinstance(error, RuntimeFault) and error.code == "FAULT_AFTER_RECEIPT" for error in failures))
        self.assertEqual(len(set(reached)), 20)
        original_effects = effect_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(original_effects), 20)
        self.assertEqual(len(set(original_effects)), 20)
        original_receipts = await self.host.store.call(lambda db: rows(db, "SELECT id,run_id,state FROM actions WHERE name='commit' ORDER BY id"))
        self.assertTrue(all(row["state"] == "completed" for row in original_receipts))
        self.assertEqual(before.calls, 20)
        await self.host.aclose()
        after = ReceiptModel()
        self.host = Host(self.root / "state", model_api=after)
        self.host.register_tool(provider)
        await self.host.__aenter__()
        resumed = await self.simultaneous([lambda run=run: self.host.runs.resume(run.id, "repair-original") for run in accepted])
        self.assertFalse(any(isinstance(value, Exception) for value in resumed))
        outputs = await asyncio.gather(*(self.host.runs.result(run.id, timeout=15) for run in accepted))
        self.assertTrue(all(output.text == "Complete" for output in outputs))
        self.assertEqual(effect_file.read_text(encoding="utf-8").splitlines(), original_effects)
        self.assertEqual(await self.host.store.call(lambda db: rows(db, "SELECT id,run_id,state FROM actions WHERE name='commit' ORDER BY id")), original_receipts)
        self.assertEqual(after.calls, 20)
        ledger = await self.host.budgets.get(budget["id"])
        self.assertEqual(ledger["usage"]["model_calls"], 40)
        self.assertEqual(ledger["usage"]["tool_calls"], 20)
        self.assertEqual(ledger["provider_reported"]["input_tokens"], 800)
        self.assertEqual(ledger["reserved"], {"input_tokens": 0, "output_tokens": 0})
