"""Independent summary configuration and durable, bounded recovery attempts."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oea.contracts.configuration import AgentDefinition, Budget, ContextPolicy, Execution, Model, SummaryPolicy
from oea.contracts.domain import Completion, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.context import RunContext
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.workspace.environment import WorkspacePaths


class SummaryAPI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def complete(self, model, messages, **options):
        self.calls.append((model, messages, options["output_tokens"]))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-summary-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://main.invalid/v1", model="main", max_output_tokens=1024),
            summary_model=Model(base_url="https://summary.invalid/v1", model="summary", thinking="enabled"),
            context=ContextPolicy(memory_tokens=256, summary=SummaryPolicy(max_output_tokens=8192, thinking="disabled")))
        await self.host.agents.save(self.definition)
        session = await self.host.sessions.create("p")
        instance = await self.host.instances.create(session["id"], "worker")
        with patch.object(self.host, "launch"):
            self.handle = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Inspect", idempotency_key="work"))
        await self.host.store.call(lambda db: db.execute("UPDATE runs SET state='running' WHERE id=?", (self.handle.id,)), transaction=True)
        self.context = await self.make_context()
        self.messages = [{"role": "system", "content": "Update memory"}, {"role": "user", "content": "Confirmed evidence"}]

    async def make_context(self):
        run, instance = await self.host.store.call(lambda db: (
            one(db, "SELECT * FROM runs WHERE id=?", (self.handle.id,)),
            one(db, "SELECT * FROM instances WHERE id=?", (self.handle.instance_id,))))
        paths = WorkspacePaths.prepare(self.definition.workspace, instance["id"], run["id"], self.root)
        return RunContext(self.host, run, instance, self.definition, paths)

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_empty_length_retries_larger_generation_and_reuses_receipts_after_restart(self):
        api = SummaryAPI([
            Completion(finish_reason="length", usage={"prompt_tokens": 20, "completion_tokens": 4096, "completion_tokens_details": {"reasoning_tokens": 4096}}),
            Completion(text="Confirmed evidence", usage={"prompt_tokens": 20, "completion_tokens": 40})])
        self.host.model_api = api
        self.assertEqual(await self.context.summarize(self.messages, 8192), "Confirmed evidence")
        self.assertEqual([call[2] for call in api.calls], [4096, 8192])
        self.assertTrue(all(call[0].model == "summary" and call[0].thinking == "disabled" for call in api.calls))
        self.assertEqual(self.definition.model.max_output_tokens, 1024)
        await self.host.aclose()
        self.host = await Host(self.root, model_api=api).__aenter__()
        self.context = await self.make_context()
        self.assertEqual(await self.context.summarize(self.messages, 8192), "Confirmed evidence")
        self.assertEqual(len(api.calls), 2)
        usage = await self.host.store.call(lambda db: rows(db, "SELECT purpose FROM usage WHERE run_id=?", (self.handle.id,)))
        self.assertEqual([row["purpose"] for row in usage], ["summary", "summary"])

    async def test_unknown_send_and_deterministic_bad_configuration_do_not_repeat(self):
        for code, retryable, facts in [("MODEL_TIMEOUT", True, {"send_started": True}),
                                      ("MODEL_HTTP_ERROR", False, {"provider_accepted": False})]:
            api = SummaryAPI([RuntimeFault(code, "Provider failure", retryable=retryable, details={"attempt_facts": facts})])
            self.host.model_api = api
            messages = self.messages + [{"role": "user", "content": code}]
            for _ in range(2):
                with self.assertRaises(RuntimeFault) as caught:
                    await self.context.summarize(messages, 8192)
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(api.calls), 1)

    async def test_rejected_retryable_attempts_stop_at_three_and_do_not_reset_on_reentry(self):
        failure = RuntimeFault("MODEL_HTTP_ERROR", "Busy", retryable=True,
            details={"attempt_facts": {"send_started": True, "provider_accepted": False}})
        api = SummaryAPI([failure, failure, failure])
        self.host.model_api = api
        for _ in range(2):
            with self.assertRaises(RuntimeFault):
                await self.context.summarize(self.messages, 8192)
        self.assertEqual(len(api.calls), 3)

    async def test_budget_exhaustion_does_not_send_recovery(self):
        policy = Budget(max_model_calls=1)
        await self.host.store.call(lambda db: db.execute("UPDATE budgets SET policy_json=? WHERE id=?", (policy.model_dump_json(), self.handle.budget_id)), transaction=True)
        api = SummaryAPI([Completion(finish_reason="length", usage={"prompt_tokens": 20, "completion_tokens": 4096})])
        self.host.model_api = api
        with self.assertRaises(RuntimeFault) as caught:
            await self.context.summarize(self.messages, 8192)
        self.assertEqual(caught.exception.code, "BUDGET_EXHAUSTED")
        self.assertEqual(len(api.calls), 1)
