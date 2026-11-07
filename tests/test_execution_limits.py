"""Persistent graph limit recovery retains work, budgets and idempotency."""

import json
from pathlib import Path
import tempfile
import unittest

from oea import AgentDefinition, Completion, Execution, Model, RunSpec, WorkflowBuilder
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-limits-")
        self.calls = 0
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                self.calls += 1
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host = await Host(Path(self.temporary.name) / "state", model_api=ModelFixture()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="fixture"), execution=Execution(max_steps=1)))
        session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(session["id"], "worker")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def failed_run(self):
        run = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Finish", idempotency_key="work"))
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(caught.exception.code, "EXECUTION_STEP_LIMIT")
        self.assertEqual(caught.exception.details["effective_max_steps"], 1)
        self.assertEqual(caught.exception.details["unit"], "super_step")
        self.assertTrue(caught.exception.identity["evidence_ref"]["checkpoint"])
        return run

    async def test_run_limit_adjustment_reuses_budget_and_rejects_conflicting_key(self):
        run = await self.failed_run()
        await self.host.runs.resume(run.id, "continue", execution_limits={"max_steps": 20})
        self.assertEqual((await self.host.runs.result(run.id, timeout=5)).text, "Done")
        resumed = await self.host.runs.resume(run.id, "continue", execution_limits={"max_steps": 20})
        self.assertEqual(resumed["budget_id"], run.budget_id)
        self.assertEqual(self.calls, 1)
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.resume(run.id, "continue", execution_limits={"max_steps": 21})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        receipt = await self.host.store.call(lambda db: one(db, "SELECT * FROM run_resumptions WHERE run_id=?", (run.id,)))
        self.assertEqual(json.loads(receipt["execution_limits_json"])["effective"], {"max_steps": 20})
        self.assertEqual(json.loads(receipt["failure_json"])["code"], "EXECUTION_STEP_LIMIT")

    async def test_omitted_limit_reuses_latest_value_after_model_failure(self):
        from unittest.mock import patch
        run = await self.failed_run()
        async def refused(*args, **kwargs):
            raise RuntimeFault("MODEL_FAILED", "Known model failure", details={"request_sent": False})
        with patch.object(self.host.model_api, "complete", refused):
            await self.host.runs.resume(run.id, "model-only", execution_limits={"max_steps": 17})
            with self.assertRaises(RuntimeFault) as caught:
                await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(caught.exception.code, "MODEL_FAILED")
        await self.host.runs.resume(run.id, "deliver")
        self.assertEqual((await self.host.runs.result(run.id, timeout=5)).text, "Done")
        self.assertEqual(self.calls, 1)
        receipt = await self.host.store.call(lambda db: one(db, "SELECT execution_limits_json FROM run_resumptions WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run.id,)))
        self.assertEqual(json.loads(receipt["execution_limits_json"])["effective"], {"max_steps": 17})

    async def test_workflow_limit_recovery_does_not_repeat_completed_function(self):
        calls = []
        def first() -> dict:
            calls.append(1)
            return {"value": 7}
        self.host.workflows.register_function(first)
        graph = (WorkflowBuilder().node("start", "start").node("first", "function", name="first")
                 .node("end", "end", outputs={"value": "{{nodes.first.value}}"}).connect("start", "first").connect("first", "end").build(max_steps=2))
        saved = await self.host.workflows.create("p", "Work", graph)
        run = await self.host.workflows.run(saved["id"], {}, "work", project_id="p")
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.workflows.result(run["id"], project_id="p", timeout=5)
        self.assertEqual(caught.exception.code, "EXECUTION_STEP_LIMIT")
        await self.host.workflows.resume(run["id"], "continue", project_id="p", execution_limits={"max_steps": 20})
        await self.host.workflows.result(run["id"], project_id="p", timeout=5)
        self.assertEqual(calls, [1])
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.workflows.resume(run["id"], "continue", project_id="p", execution_limits={"max_steps": 21})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
