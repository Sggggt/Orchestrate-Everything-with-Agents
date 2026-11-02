"""Cache-aware cost commitments, corrections, tail reserves and ownership scopes."""

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from oea.contracts.configuration import AgentDefinition, Budget, Model, ModelPricing, Execution, Tools, Workspace
from oea.contracts.domain import RunSpec, Completion
from oea.contracts.errors import RuntimeFault
from oea.budgets.measurement import normalize, charge
from oea.runtime.host import Host
from oea.storage.persistence import one, rows


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-cost-")
        self.host = await Host(Path(self.temporary.name)).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.price = ModelPricing(currency="USD", revision="test-price", input_per_million=1,
            output_per_million=2, cache_read_per_million="0.1", cache_write_per_million=4)
        self.model = Model(base_url="https://model.invalid/v1", model="fixture", pricing=self.price)
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=self.model))
        session = await self.host.sessions.create("p")
        self.session = session
        shared = await self.host.budgets.create("p", Budget(max_cost="0.1"), "shared", session_id=session["id"])
        self.budget = shared["id"]
        self.runs = []
        for name in ("first", "second"):
            instance = await self.host.instances.create(session["id"], "worker", name=name)
            with patch.object(self.host, "launch"):
                handle = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Inspect", idempotency_key="work", budget_id=self.budget))
            self.runs.append(await self.host.store.call(lambda db: one(db, "SELECT * FROM runs WHERE id=?", (handle.id,))))

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def reserve(self, identity, run=0, incoming=1000, outgoing=100, model=None):
        return await self.host.store.call(lambda db: self.host.budgets.model(db, self.runs[run], identity, "model", incoming, outgoing, model=model or self.model), transaction=True)

    async def settle(self, identity, raw):
        await self.host.store.call(lambda db: self.host.budgets.settle(db, identity, raw), transaction=True)

    async def test_cache_read_and_reasoning_are_not_charged_twice(self):
        await self.reserve("one")
        raw = {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 900},
               "completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 80}}
        await self.settle("one", raw)
        report = await self.host.budgets.get(self.budget)
        self.assertEqual(report["usage"]["input_total"], 1000)
        self.assertEqual(report["usage"]["input_uncached"], 100)
        self.assertEqual(report["usage"]["reasoning"], 80)
        self.assertEqual(report["cost"]["currencies"]["USD"]["settled_micros"], 390)
        prior = await self.host.store.call(lambda db: one(db, "SELECT * FROM usage_measurements WHERE usage_id='one'"))
        await self.settle("one", raw)
        self.assertEqual(prior, await self.host.store.call(lambda db: one(db, "SELECT * FROM usage_measurements WHERE usage_id='one'")))
        await self.settle("one", {**raw, "completion_tokens": 110})
        revised = await self.host.budgets.get(self.budget)
        self.assertEqual(revised["usage"]["model_calls"], 1)
        self.assertEqual(revised["cost"]["currencies"]["USD"]["settled_micros"], 410)
        self.assertEqual(len(await self.host.store.call(lambda db: rows(db, "SELECT * FROM events WHERE kind='usage.corrected'"))), 1)

    async def test_missing_cache_is_unknown_and_cost_uses_explicit_upper_bound(self):
        await self.reserve("one")
        await self.settle("one", {"prompt_tokens": 1000, "completion_tokens": 100})
        report = await self.host.budgets.get(self.budget)
        self.assertIsNone(report["usage"]["input_uncached"])
        self.assertIsNone(report["usage"]["cache_read"])
        self.assertEqual(report["cost"]["currencies"]["USD"]["estimated_micros"], 1200)
        unbounded = self.model.model_copy(update={"pricing": self.price.model_copy(update={"cache_read_per_million": None})})
        with self.assertRaises(RuntimeFault) as caught:
            await self.reserve("unknown-price", model=unbounded)
        self.assertEqual(caught.exception.code, "PRICE_UNAVAILABLE")

    async def test_separate_cache_creation_and_missing_usage(self):
        raw = {"input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100, "output_tokens": 100}
        normalized = normalize(raw, "anthropic")
        self.assertEqual(normalized["input_total"], 1000)
        self.assertEqual(charge(normalized, self.price, estimated_input=1000, estimated_output=100), (780, "settled"))
        await self.reserve("missing")
        await self.settle("missing", {})
        report = await self.host.budgets.get(self.budget)
        self.assertEqual(report["needs_review"]["model_calls"], 1)
        self.assertIsNone(report["provider_reported"]["input_tokens"])
        self.assertEqual(report["usage"]["input_tokens"], 1000)

    async def test_run_direct_and_root_do_not_grow_with_a_shared_sibling(self):
        await self.reserve("first")
        await self.settle("first", {"prompt_tokens": 100, "completion_tokens": 10})
        before = await self.host.budgets.for_run(self.runs[0])
        await self.reserve("second", run=1)
        await self.settle("second", {"prompt_tokens": 200, "completion_tokens": 20})
        after = await self.host.budgets.for_run(self.runs[0])
        for scope in ("run_direct", "root_work"):
            self.assertEqual(before[scope]["usage"], after[scope]["usage"])
        self.assertEqual(after["shared_budget"]["usage"]["model_calls"], 2)
        self.assertFalse(after["run_direct"]["includes_children"])

    async def test_parallel_reservation_cannot_overspend_and_output_is_reduced_to_fit(self):
        from asyncio import gather
        policy = Budget(max_cost="0.0015", finalization_fraction=0, recovery_fraction=0)
        await self.host.store.call(lambda db: db.execute("UPDATE budgets SET policy_json=? WHERE id=?", (policy.model_dump_json(), self.budget)), transaction=True)
        results = await gather(self.reserve("first", outgoing=1000), self.reserve("second", run=1, outgoing=1000), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, RuntimeFault) for result in results), 1)
        self.assertIn(250, results)
        report = await self.host.budgets.get(self.budget)
        self.assertEqual(report["cost"]["currencies"]["USD"]["accounted_micros"], 1500)

    async def test_finalization_and_recovery_reserves_are_within_original_budget(self):
        policy = Budget(max_output_tokens=1000, finalization_fraction=.2, recovery_fraction=.1)
        await self.host.store.call(lambda db: db.execute("UPDATE budgets SET policy_json=? WHERE id=?", (policy.model_dump_json(), self.budget)), transaction=True)
        await self.reserve("first", outgoing=600)
        await self.settle("first", {"prompt_tokens": 10, "completion_tokens": 600})
        allowance, hint = await self.host.store.call(lambda db: self.host.budgets.headroom(db, self.runs[0], 200))
        self.assertEqual(hint["phase"], "finalizing")
        self.assertEqual(allowance, 200)
        self.assertEqual(hint["remaining_output_tokens"], 400)
        self.assertTrue(hint["instruction"])

    async def cost_tail_run(self, *, partial=False):
        calls = []
        class API:
            async def complete(self, model, messages, **options):
                calls.append((messages, options["output_tokens"]))
                tools = [{"id":name,"type":"function","function":{"name":name,"arguments":json.dumps(arguments)}}
                    for name, arguments in (("apply_patch",{"operation":"add","path":"partial.txt","content":"retained"}),
                                            ("publish_artifact",{"path":"partial.txt"}))] if partial else []
                return Completion(text="" if partial else "Saved results", tool_calls=tuple(tools), finish_reason="tool_calls" if partial else "stop",
                    usage={"prompt_tokens":100,"prompt_tokens_details":{"cached_tokens":0},"completion_tokens":options["output_tokens"] if partial else 10})
        self.host.model_api = API()
        price = ModelPricing(currency="USD", revision="tail-test", input_per_million=0, output_per_million=1,
                             cache_read_per_million=0, cache_write_per_million=0)
        await self.host.agents.save(AgentDefinition(id="tail", name="Tail", project_id="p",
            model=self.model.model_copy(update={"pricing":price,"max_output_tokens":1000}),
            tools=Tools(local=("apply_patch","publish_artifact")) if partial else Tools(), workspace=Workspace(sandbox="trusted"),
            execution=Execution(budget=Budget(max_cost="0.001", max_output_tokens=100000, finalization_fraction=.2, recovery_fraction=.1))))
        instance = await self.host.instances.create(self.session["id"], "tail")
        handle = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Retain the available result", idempotency_key="tail"))
        return handle, calls

    async def test_large_first_request_receives_cost_tail_guidance_before_any_send(self):
        handle, calls = await self.cost_tail_run()
        result = await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 900)
        self.assertIn("Stop expanding the work", calls[0][0][0]["content"])
        self.assertEqual(result.usage["run_direct"]["usage"]["model_calls"], 1)
        phases = await self.host.store.call(lambda db: rows(db, "SELECT m.budget_phase FROM usage_measurements m JOIN usage u ON m.usage_id=u.id WHERE u.run_id=?", (handle.id,)))
        self.assertEqual(phases, [{"budget_phase":"finalizing"}])

    async def test_cost_boundary_keeps_saved_partial_artifacts_and_recovery_reserve(self):
        handle, calls = await self.cost_tail_run(partial=True)
        with self.assertRaises(RuntimeFault) as failure:
            await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(failure.exception.code, "BUDGET_EXHAUSTED")
        self.assertEqual(len(calls), 1)
        artifacts = await self.host.artifacts.list(self.session["id"], "p")
        self.assertEqual(len(artifacts["items"]), 1)
        self.assertEqual(artifacts["items"][0]["run_id"], handle.id)
        self.assertEqual((await self.host.artifacts.read(artifacts["items"][0]["id"], "p"))["text"], "retained")
        run = await self.host.store.call(lambda db: one(db, "SELECT * FROM runs WHERE id=?", (handle.id,)))
        used = await self.host.store.call(lambda db: self.host.budgets.totals(db, run["budget_id"]))
        self.assertEqual(used["cost"], 900)
        reserve = await self.host.store.call(lambda db: self.host.budgets.model(db, run, "recovery-tail", "summary", 0, 1000,
            model=Model(base_url="https://model.invalid",model="summary",pricing=ModelPricing(currency="USD",revision="tail-test",input_per_million=0,output_per_million=1,cache_read_per_million=0,cache_write_per_million=0))), transaction=True)
        self.assertEqual(reserve, 100)
