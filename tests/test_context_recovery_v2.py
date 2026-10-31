"""Real transport, graph checkpoints and accounting for bounded window recovery."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from oea import AgentDefinition, Budget, Execution, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.models.api import ModelAPI
from oea.runtime.context import RunContext
from oea.runtime.host import Host
from oea.storage.persistence import encode, one, rows
from test_model_api_v2 import Stream, Transport, frame


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-recovery-")
        self.root = Path(self.temporary.name) / "state"
        self.requests = []
        self.status, self.code, self.always_reject = 400, "context_length_exceeded", False
        self.transport_timeout = False
        self.first_stream = None
        def handle(request):
            self.requests.append(json.loads(request.content))
            if self.transport_timeout:
                raise httpx.ReadTimeout("Synthetic timeout")
            if len(self.requests) == 1 and self.first_stream is not None:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(self.first_stream))
            if len(self.requests) == 1 or self.always_reject:
                return httpx.Response(self.status, json={"error": {"code": self.code, "message": "Request exceeds context length"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream([
                frame({"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 2}})]))
        self.transport = Transport(handle)
        self.api = ModelAPI(self.transport, secret=lambda _: None)
        self.host = await Host(self.root, model_api=self.api).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.configure()
        session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(session["id"], "worker")

    async def configure(self, calls=128):
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="fixture"),
            execution=Execution(budget=Budget(max_model_calls=calls))))

    async def asyncTearDown(self):
        await self.host.aclose()
        await self.transport.client.aclose()
        self.temporary.cleanup()

    async def submit(self, history=True):
        with patch.object(self.host, "launch"):
            run = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Finish", idempotency_key="work"))
        if history:
            def seed(db):
                for index in range(12):
                    db.execute("INSERT INTO conversation(id,instance_id,run_id,role,body_json,created_at) VALUES(?,?,?,'user',?,?)",
                               ("history-" + str(index), self.instance["id"], run.id, encode({"role": "user", "content": "Evidence " * 600}), index))
            await self.host.store.call(seed, transaction=True)
        self.host.launch(run.id)
        return run

    async def test_confirmed_rejection_reduces_input_and_keeps_action_and_budget(self):
        run = await self.submit()
        self.assertEqual((await self.host.runs.result(run.id, timeout=10)).text, "Done")
        self.assertEqual(len(self.requests), 2)
        self.assertLess(len(encode(self.requests[1]["messages"])), len(encode(self.requests[0]["messages"])))
        actions = await self.host.store.call(lambda db: rows(db, "SELECT * FROM actions WHERE run_id=?", (run.id,)))
        self.assertEqual(len(actions), 1)
        archived = await self.host.runs.attempts(actions[0]["id"], project_id="p")
        self.assertEqual(len(archived["items"]), 1)
        self.assertFalse(archived["items"][0]["attempt_facts"]["provider_accepted"])
        budget = await self.host.budgets.get(run.budget_id)
        self.assertEqual(budget["usage"]["model_calls"], 2)
        self.assertEqual(budget["needs_review"]["model_calls"], 0)
        self.assertEqual(budget["provider_reported"]["input_tokens"], 100)

    async def test_second_window_rejection_does_not_loop(self):
        self.always_reject = True
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_CONTEXT_REJECTED")
        self.assertEqual(len(self.requests), 2)

    async def test_unknown_code_and_untrusted_text_never_retry(self):
        self.code = "invalid_parameter"
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_HTTP_ERROR")
        self.assertEqual(len(self.requests), 1)

    async def test_known_code_with_server_failure_status_does_not_retry(self):
        self.status = 500
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_HTTP_ERROR")
        self.assertEqual(len(self.requests), 1)

    async def test_timeout_does_not_authorize_window_recovery(self):
        self.transport_timeout = True
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_TIMEOUT")
        self.assertEqual(len(self.requests), 1)

    async def test_stream_error_after_text_and_tool_fragments_never_retries(self):
        self.first_stream = [frame({"choices": [{"delta": {"content": "partial", "tool_calls": [{"index": 0, "id": "call", "function": {"name": "read_context", "arguments": "{}"}}]}}]}),
                             frame({"error": {"code": "context_length_exceeded"}})]
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_PROVIDER_ERROR")
        self.assertEqual(len(self.requests), 1)
        action = await self.host.runs.action(caught.exception.identity["action_id"], limit=16384)
        result = json.loads(action["text"])
        self.assertEqual(result["message"]["content"], "partial")
        self.assertEqual(len(result["discarded_tool_calls"]), 1)

    async def test_cancel_at_recovery_boundary_prevents_a_second_send(self):
        ready = asyncio.Event()
        async def wait(context, state):
            ready.set()
            await asyncio.Event().wait()
        with patch.object(RunContext, "recover_context", wait):
            run = await self.submit()
            await asyncio.wait_for(ready.wait(), 5)
            cancelled = await self.host.runs.cancel(run.id)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(cancelled["stop_confirmed"])
        self.assertEqual(len(self.requests), 1)

    async def test_unsettled_side_effect_blocks_automatic_model_recovery(self):
        recover = RunContext.recover_context
        async def pending(context, state):
            value = await recover(context, state)
            await context.host.store.call(lambda db: db.execute(
                "INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,created_at,updated_at) VALUES('pending',?,'pending','external','hash','{}','external','needs_review',1,1)",
                (context.run["id"],)), transaction=True)
            return value
        with patch.object(RunContext, "recover_context", pending):
            run = await self.submit()
            with self.assertRaises(RuntimeFault) as caught:
                await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_REVIEW_REQUIRED")
        self.assertEqual((await self.host.runs.get(run.id))["state"], "needs_review")
        self.assertEqual(len(self.requests), 1)

    async def test_fixed_input_without_reduction_preserves_original_failure(self):
        run = await self.submit(history=False)
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_CONTEXT_REJECTED")
        self.assertEqual(len(self.requests), 1)

    async def test_exhausted_root_budget_blocks_second_send(self):
        await self.configure(calls=1)
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(caught.exception.code, "BUDGET_EXHAUSTED")
        self.assertEqual(len(self.requests), 1)

    async def test_restart_after_recovery_commit_reuses_evidence(self):
        recover = RunContext.recover_context
        async def crash(context, state):
            await recover(context, state)
            raise RuntimeFault("TEST_INTERRUPTED", "Stopped after recovery evidence")
        with patch.object(RunContext, "recover_context", crash):
            run = await self.submit()
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(run.id, timeout=10)
        await self.host.aclose()
        self.host = await Host(self.root, model_api=self.api).__aenter__()
        await self.host.runs.resume(run.id, "continue")
        self.assertEqual((await self.host.runs.result(run.id, timeout=10)).text, "Done")
        self.assertEqual(len(self.requests), 2)
        evidence = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events WHERE run_id=? AND kind='context.recovered'", (run.id,)))
        self.assertEqual(evidence["count"], 1)
