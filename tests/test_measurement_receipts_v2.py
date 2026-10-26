"""Native request facts and shared usage projections with an HTTP protocol stub."""

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from oea import AgentDefinition, Model, RunSpec, Budget
from oea.contracts.configuration import ContextPolicy
from oea.contracts.errors import RuntimeFault
from oea.models.api import ModelAPI
from oea.models.request import prepare_request
from oea.runtime.host import Host
from oea.storage.persistence import one, rows


class Transport:
    def __init__(self, handler):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @asynccontextmanager
    async def borrow(self, base_url):
        yield self.client


class MeasurementReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-measurement-")
        self.incomplete_tools = False
        self.requests, self.report, self.status, self.provider_code = [], {"prompt_tokens": 12, "completion_tokens": 3}, 200, "unspecified"
        def handle(request):
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"code": self.provider_code, "message": "Request rejected"}})
            if self.incomplete_tools:
                value = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "candidate", "function": {"name": "read_file", "arguments": "{}"}}]}}]}
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: " + json.dumps(value) + "\n\n")
            chunks = [{"choices": [{"delta": {"content": "Complete"}, "finish_reason": "stop"}]}, {"choices": [], "usage": self.report}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join("data: " + json.dumps(value) + "\n\n" for value in chunks) + "data: [DONE]\n\n")
        self.transport = Transport(handle)
        self.api = ModelAPI(self.transport, secret=lambda reference: None)
        self.host = await Host(Path(self.temporary.name) / "state", model_api=self.api).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://example.invalid/v1", model="fixture"))
        await self.host.agents.save(self.definition)
        self.session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(self.session["id"], "worker", name="one")

    async def asyncTearDown(self):
        await self.host.aclose()
        await self.transport.client.aclose()
        self.temporary.cleanup()

    async def submit(self):
        return await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Check", idempotency_key="work"))

    async def action(self, run):
        return await self.host.store.call(lambda db: one(db, "SELECT * FROM actions WHERE run_id=? AND name='model.complete'", (run.id,)))

    async def test_exact_request_bytes_and_bounded_stage_facts(self):
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        action = await self.action(run)
        metadata, result = json.loads(action["input_json"]), json.loads(action["result_json"])
        self.assertEqual(metadata["request_measurement"]["value"], len(self.requests[0].content))
        self.assertTrue(result["attempt_facts"]["send_started"])
        self.assertTrue(result["attempt_facts"]["response_received"])
        self.assertIsNone(result["attempt_facts"]["provider_accepted"])
        facts = await self.host.store.call(lambda db: rows(db, "SELECT kind FROM events WHERE run_id=? AND kind IN ('model.prepared','model.send_started','model.response_received') ORDER BY position", (run.id,)))
        self.assertEqual([value["kind"] for value in facts], ["model.prepared", "model.send_started", "model.response_received"])
        budget = await self.host.budgets.get(run.budget_id)
        self.assertEqual(budget["provider_reported"]["input_tokens"], 12)
        self.assertEqual(budget["attempts"]["responses_received"], 1)
        page = await self.host.runs.action(action["id"], limit=12)
        self.assertEqual(page["offset_unit"], "unicode_codepoint")
        self.assertTrue(page["content_view"]["source_complete"])
        self.assertIsNotNone(page["next_offset"])

    async def test_pre_send_rejection_and_explicit_retry_keep_distinct_attempts(self):
        with patch("oea.models.request.MODEL_REQUEST_MAX_BYTES", 1):
            run = await self.submit()
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(self.requests, [])
        first = await self.action(run)
        old_id = json.loads(first["input_json"])["attempt"]["attempt_id"]
        budget = await self.host.budgets.get(run.budget_id)
        self.assertEqual(budget["attempts"]["rejected_before_send"], 1)
        self.assertIsNone(budget["provider_reported"]["input_tokens"])
        self.assertEqual(budget["estimated"]["model_calls"], 0)
        await self.host.runs.resume(run.id, "retry", input="Continue")
        await self.host.runs.result(run.id, timeout=10)
        current = await self.action(run)
        self.assertEqual(current["id"], first["id"])
        self.assertNotEqual(json.loads(current["input_json"])["attempt"]["attempt_id"], old_id)
        old = await self.host.runs.action(first["id"], attempt_id=old_id)
        self.assertFalse(old["is_current"])
        self.assertFalse(old["attempt_facts"]["send_started"])
        self.assertEqual(old["error"]["diagnostic"]["origin"], "local")

    async def test_partial_usage_and_cache_subset_are_not_zero_filled(self):
        self.report = {"prompt_tokens": 12, "prompt_tokens_details": {"cached_tokens": 4}}
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        budget = await self.host.budgets.get(run.budget_id)
        self.assertEqual(budget["provider_reported"]["input_tokens"], 12)
        self.assertEqual(budget["provider_reported"]["cached_input_tokens"], 4)
        self.assertIsNone(budget["provider_reported"]["output_tokens"])
        self.assertEqual(budget["estimated"]["model_calls"], 1)
        self.assertGreater(budget["usage"]["output_tokens"], 0)

    async def test_invalid_cache_report_is_not_silently_relabelled(self):
        self.report = {"prompt_tokens": 12, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 20}}
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        budget = await self.host.budgets.get(run.budget_id)
        self.assertIsNone(budget["provider_reported"]["cached_input_tokens"])
        self.assertEqual(budget["provider_reported"]["invalid_cache_reports"], 1)
        self.assertEqual(budget["usage"]["input_tokens"], 12)

    async def test_http_status_alone_is_not_a_quota_diagnosis(self):
        self.status = 403
        run = await self.submit()
        with self.assertRaises(RuntimeFault):
            await self.host.runs.result(run.id, timeout=10)
        action = await self.action(run)
        error = json.loads(action["error_json"])
        self.assertEqual(error["diagnostic"]["reason"], "unknown")
        self.assertTrue(error["diagnostic"]["response_received"])
        budget = await self.host.budgets.get(run.budget_id)
        self.assertEqual(budget["needs_review"]["model_calls"], 1)
        self.assertIsNone(budget["provider_reported"]["input_tokens"])

    async def test_valid_looking_unfinished_tool_json_is_retained_but_never_executed(self):
        self.incomplete_tools = True
        run = await self.submit()
        with self.assertRaises(RuntimeFault):
            await self.host.runs.result(run.id, timeout=10)
        action = await self.action(run)
        result = json.loads(action["result_json"])
        self.assertNotIn("tool_calls", result["message"])
        self.assertEqual(result["discarded_tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(result["content_view"]["integrity"], "partial")
        tool_calls = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM actions WHERE run_id=? AND name!='model.complete'", (run.id,)))
        self.assertEqual(tool_calls["count"], 0)

    async def test_actual_output_reservation_drives_the_effective_input_window(self):
        definition = self.definition.model_copy(deep=True)
        definition.model.context_window_tokens = 6200
        definition.execution.budget = Budget(max_output_tokens=64)
        definition.instructions.system = "Current facts " * 300
        await self.host.agents.save(definition, expected_revision=1)
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        metadata = json.loads((await self.action(run))["input_json"])
        self.assertEqual(metadata["projection"]["output_reserved_tokens"], 64)
        self.assertEqual(metadata["projection"]["budget_tokens"], 6200 - 64 - 1024)
        self.assertGreater(metadata["projection"]["input_tokens"], 6200 - 4096 - 1024)

    async def test_native_summary_uses_its_own_reservation_before_sending(self):
        previous = await self.submit()
        await self.host.runs.result(previous.id, timeout=10)
        for index in range(32):
            await self.host.contexts.append({"id": previous.id}, self.instance,
                {"role": "user", "content": "Retained source evidence " * 100}, "prior-" + str(index))
        definition = self.definition.model_copy(deep=True)
        definition.model = Model(base_url="https://example.invalid/v1", model="fixture", context_window_tokens=10000, max_output_tokens=6000)
        definition.context = ContextPolicy(memory_tokens=256)
        await self.host.agents.save(definition, expected_revision=1)
        current = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Continue with retained evidence", idempotency_key="next"))
        await self.host.runs.result(current.id, timeout=10)
        summaries = await self.host.store.call(lambda db: rows(db, "SELECT input_json FROM actions WHERE run_id=? AND json_extract(input_json,'$.purpose')='summary'", (current.id,)))
        self.assertTrue(summaries)
        for row in summaries:
            projection = json.loads(row["input_json"])["projection"]
            self.assertEqual(projection["output_reserved_tokens"], 256)
            self.assertLessEqual(projection["input_tokens"], 10000 - 256 - 1024)

    async def test_missing_historical_facts_remain_unknown_after_reopen(self):
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        action = await self.action(run)
        await self.host.store.call(lambda db: db.execute("UPDATE actions SET input_json=json_remove(input_json,'$.attempt') WHERE id=?", (action["id"],)), transaction=True)
        await self.host.aclose()
        self.host = await Host(Path(self.temporary.name) / "state", model_api=self.api).__aenter__()
        budget = await self.host.budgets.get(run.budget_id)
        self.assertEqual(budget["attempts"]["historical_unknown"], 1)
        self.assertEqual(budget["attempts"]["send_coverage"], 0)
        self.assertIsNone((await self.host.runs.action(action["id"]))["attempt_facts"])

    async def test_captured_log_artifact_never_claims_complete_original_output(self):
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        identity, action_id = "artifact_capture", "command_capture"
        directory = self.host.root / "artifacts"
        directory.mkdir(exist_ok=True)
        path = directory / identity
        path.write_text("saved", encoding="utf-8")
        def seed(db):
            db.execute("INSERT INTO artifacts VALUES(?,'p',?,?,?,?,'hash',5,'text/plain',1,?,'run')", (identity, self.instance["id"], run.id, action_id + ".stdout.txt", str(path), self.session["id"]))
            receipt = json.dumps({"output_refs": [{"artifact": {"id": identity}, "truncated": True, "observed_bytes": 500}]})
            db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,created_at,updated_at) VALUES(?,?,?,'run_command','hash','{}','local','completed',?,1,1)", (action_id, run.id, action_id, receipt))
        await self.host.store.call(seed, transaction=True)
        metadata = await self.host.artifacts.get(identity)
        self.assertFalse(metadata["content_view"]["source_complete"])
        page = await self.host.artifacts.read(identity, "p", limit=64)
        self.assertEqual(page["content_view"]["integrity"], "truncated")
        self.assertEqual(page["offset_unit"], "byte")

    def test_compact_unicode_request_exact_boundary(self):
        messages = [{"role": "user", "content": "中文 and emoji 🙂"}]
        request = prepare_request(self.definition.model, messages)
        with patch("oea.models.request.MODEL_REQUEST_MAX_BYTES", len(request.content)):
            self.assertEqual(prepare_request(self.definition.model, messages).content, request.content)
        with patch("oea.models.request.MODEL_REQUEST_MAX_BYTES", len(request.content) - 1):
            with self.assertRaises(RuntimeFault) as caught:
                prepare_request(self.definition.model, messages)
        self.assertEqual(caught.exception.as_dict()["measurement"]["decision"], "reject")
