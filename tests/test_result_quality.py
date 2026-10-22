"""Delivery requirements against real completion commits and temporary state."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.llm import LLMApiAgent
from ma_runtime import (AdapterEvent, AgentConfig, AgentDescriptor, ProbeResult, ResultPolicy,
                        Runtime, RuntimeFault, TaskResult)
from ma_runtime.execution_policy import request_hash
from ma_runtime.models import TaskSpec, content_hash
from ma_runtime.tools import ToolSpec


class UncertainTool:
    def describe(self):
        return ToolSpec(name="uncertain_tool", side_effects="EXTERNAL")

    async def invoke(self, request):
        raise ConnectionError("Result unavailable after dispatch")


class ResultAgent:
    def __init__(self):
        self.boundaries = {}

    def describe(self):
        return AgentDescriptor(agent_id="worker", max_concurrency=4)

    async def execute(self, request, context):
        result = TaskResult.model_validate(request.payload.get("result", {}))
        if request.payload.get("uncertain_tool"):
            try:
                await context.invoke_tool("uncertain_tool", {}, request.task_id + ":tool")
            except RuntimeFault:
                pass  # A delivered result must not conceal an unresolved tool action.
        if request.payload.get("publish"):
            artifact = await context.put_bytes("report.txt", b"Completed result")
            result = result.model_copy(update={"artifact_ids": (artifact.id,)})
        boundary = AdapterEvent(type="completed", result=result)
        self.boundaries[request.run_id] = boundary
        yield boundary

    async def probe(self, run_ref):
        return ProbeResult(state="STOPPED", confirmed_stopped=True)

    async def aclose(self):
        pass


class CompletionAgent(LLMApiAgent):
    def __init__(self, config, response):
        super().__init__(config)
        self.response = response

    async def _completion(self, messages, context, tools=None):
        yield {"calls": [], "text": "", "finish_reason": "stop", **self.response}


class PartialFailureAgent(ResultAgent):
    def describe(self):
        return super().describe().model_copy(update={"supports_probe": False})

    async def execute(self, request, context):
        context.partial_model_result = TaskResult(output={"text": "Retained evidence"})
        raise RuntimeFault("REVIEW_INTERRUPTED", "Review stopped before its boundary")
        yield  # Keep the adapter's asynchronous event-stream contract.


class ResultQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-result-quality-")
        self.root = Path(self.temp.name)
        self.worker = ResultAgent()
        self.rt = await Runtime.open(self.root / "state", adapters={"worker": self.worker}).__aenter__()
        self.session = await self.rt.sessions.create()
        self.sequence = 0

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def submit(self, payload=None, policy=None, agent_id="worker"):
        self.sequence += 1
        thread = await self.rt.threads.create(self.session.id, agent_id)
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": thread.id,
            "agent_id": agent_id, "idempotency_key": str(self.sequence), "payload": payload or {},
            "result_policy": policy or {}})
        try:
            await self.rt.tasks.result(task.id, timeout=5)
        except RuntimeFault as exc:
            self.assertEqual(exc.code, "TASK_FAILED")
        return await self.rt.tasks.get(task.id)

    async def native(self, response, policy=None):
        config = AgentConfig(id="model", name="Model", kind="llm", base_url="http://model.invalid/v1", model="test")
        await self.rt.agents.configure(config)
        self.rt.adapters[config.id] = CompletionAgent(config, response)
        return await self.submit({"input": "Return the requested result"}, policy, config.id)

    async def test_committed_native_boundaries_do_not_accumulate_across_waves(self):
        config = AgentConfig(id="model", name="Model", kind="llm", base_url="http://model.invalid/v1", model="test")
        await self.rt.agents.configure(config)
        adapter = CompletionAgent(config, {"text": "Retained evidence"})
        self.rt.adapters[config.id] = adapter
        first = None
        for _ in range(3):
            for _ in range(8):
                task = await self.submit({"input": "Review the evidence"}, agent_id=config.id)
                first = first or task.id
            self.assertEqual(len(adapter.boundaries), 0)
        self.assertEqual((await self.rt.tasks.result(first)).output["text"], "Retained evidence")
        with patch.object(self.rt, "commit_boundary", side_effect=RuntimeFault("COMMIT_UNAVAILABLE", "Commit not confirmed")):
            task = await self.submit({"input": "Keep the unconfirmed boundary"}, agent_id=config.id)
        self.assertIn(task.runs[-1].id, adapter.boundaries)

    async def test_empty_and_truncated_model_results_fail_without_losing_delivery(self):
        for response, code in [({"text": "  "}, "EMPTY_OUTPUT"),
                               ({"text": "Partial result", "finish_reason": "length"}, "OUTPUT_TRUNCATED"),
                               ({"finish_reason": "length", "usage": {"completion_tokens": 10}}, "OUTPUT_TRUNCATED"),
                               ({"finish_reason": "refusal"}, "MODEL_REFUSED")]:
            with self.subTest(code=code, response=response):
                task = await self.native(response)
                self.assertEqual(task.state, "FAILED")
                self.assertEqual(task.execution_state, "SUCCEEDED")
                self.assertEqual(task.runs[0].execution_state, "SUCCEEDED")
                self.assertEqual(task.error["code"], code)
                self.assertEqual(task.result.output["text"], response.get("text", ""))
                self.assertEqual(task.result.metadata["acceptance"]["state"], "FAILED")
                self.assertEqual(task.runs[0].boundary_receipt["state"], "FAILED")
                self.assertIsNone((await self.rt.threads.get(task.thread_id)).owner_task_id)
                view = await self.rt.sessions.snapshot(self.session.id, presentation=True)
                self.assertNotIn(task.thread_id, view["failed_thread_ids"])
                self.assertEqual(next(item for item in view["tasks"] if item["id"] == task.id)["execution_state"], "SUCCEEDED")
                trace = await self.rt.tasks.trace(task.id)
                self.assertEqual(trace["runs"][-1]["execution_state"], "SUCCEEDED")
                conversation = await self.rt.threads.conversation(task.thread_id)
                replies = [item for item in conversation.items if item.role == "assistant"]
                self.assertTrue(replies)
                self.assertEqual(replies[-1].execution_state, "SUCCEEDED")
                if "usage" in response:
                    self.assertEqual(task.result.metadata["model_completion"]["usage"], response["usage"])

    async def test_plain_markdown_and_explicit_structured_results_pass(self):
        task = await self.native({"text": "Checked the requested paragraph."})
        self.assertEqual(task.state, "SUCCEEDED")
        self.assertEqual(task.result.metadata["acceptance"]["semantic_review"], "UNVERIFIED")
        task = await self.submit({"result": {"output": {"text": "Further work is needed.", "status": "failed"}}})
        self.assertEqual(task.state, "SUCCEEDED")
        self.assertEqual(task.execution_state, "SUCCEEDED")
        task = await self.native({"text": json.dumps({"answer": 4, "covered_items": ["item"]})},
            {"kind": "structured", "required_items": ["item"], "output_schema": {
                "type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}})
        self.assertEqual(task.state, "SUCCEEDED")
        self.assertEqual(task.result.output["answer"], 4)

    async def test_artifact_only_delivery_passes_and_missing_artifact_fails(self):
        policy = {"kind": "artifacts", "required_artifacts": ["report.txt"]}
        task = await self.submit({"publish": True}, policy)
        self.assertEqual(task.state, "SUCCEEDED")
        self.assertEqual((await self.rt.artifacts.get(task.result.artifact_ids[0])).name, "report.txt")
        task = await self.submit({}, policy)
        self.assertEqual(task.error["code"], "ARTIFACT_REQUIREMENT_UNMET")

    async def test_schema_and_coverage_failures_keep_the_partial_result(self):
        for policy, code in [({"required_items": ["missing"]}, "COVERAGE_REQUIREMENT_UNMET"),
            ({"output_schema": {"type": "object", "required": ["answer"]}}, "OUTPUT_SCHEMA_MISMATCH")]:
            task = await self.submit({"result": {"output": {"covered_items": ["present"]}}}, policy)
            self.assertEqual(task.error["code"], code)
            self.assertEqual(task.result.output, {"covered_items": ["present"]})
        task = await self.native({"text": "Ordinary prose"}, {"kind": "structured"})
        self.assertEqual(task.error["code"], "OUTPUT_SCHEMA_MISMATCH")

    async def test_completed_failure_receipt_is_idempotent_and_survives_restart(self):
        task = await self.submit({"result": {"output": {"text": ""}}}, {"kind": "text"})
        run = task.runs[0]
        receipt = await self.rt.commit_boundary(run.id, self.worker.boundaries[run.id], run.boundary_seq)
        self.assertEqual(receipt, run.boundary_receipt)
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state", adapters={"worker": self.worker}).__aenter__()
        stored = await self.rt.tasks.get(task.id)
        self.assertEqual(stored.state, "FAILED")
        self.assertEqual(stored.result.metadata["acceptance"]["state"], "FAILED")

    async def test_instance_members_inherit_result_requirements(self):
        batch = await self.rt.instances.spawn({"session_id": self.session.id, "agent_ids": ["worker"],
            "payloads": [{"result": {"output": {"text": "Ready"}}}, {}],
            "idempotency_key": "batch", "result_policy": {"kind": "text"}})
        async with asyncio.timeout(5):
            while (await self.rt.instances.get(batch["id"]))["state"] == "RUNNING":
                await asyncio.sleep(0.01)
        result = await self.rt.instances.get(batch["id"], include_outputs=True)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual([member["state"] for member in result["instances"]], ["SUCCEEDED", "FAILED"])

    async def test_unresolved_tool_takes_precedence_over_delivery_failure(self):
        self.rt.tools.register(UncertainTool(), allowed_agent_ids=["worker"])
        thread = await self.rt.threads.create(self.session.id, "worker")
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": thread.id,
            "agent_id": "worker", "idempotency_key": "uncertain", "payload": {"uncertain_tool": True},
            "result_policy": {"kind": "text"}})
        with self.assertRaises(RuntimeFault) as fault:
            await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(fault.exception.code, "OUTCOME_UNCERTAIN")
        state = await self.rt.tasks.get(task.id)
        self.assertEqual(state.state, "UNCERTAIN")
        self.assertIsNone(state.runs[0].boundary_receipt)
        self.assertEqual((await self.rt.threads.get(thread.id)).owner_task_id, task.id)

    async def uncertain_partial(self):
        self.rt.adapters["worker"] = PartialFailureAgent()
        thread = await self.rt.threads.create(self.session.id, "worker")
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": thread.id,
            "agent_id": "worker", "idempotency_key": "partial", "result_policy": {"kind": "text"}})
        with self.assertRaises(RuntimeFault) as failure:
            await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(failure.exception.code, "OUTCOME_UNCERTAIN")
        return await self.rt.tasks.get(task.id)

    async def test_failed_reconciliation_preserves_partial_result_and_error(self):
        before = await self.uncertain_partial()
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state", adapters={"worker": PartialFailureAgent()}).__aenter__()
        recovered = await self.rt.tasks.get(before.id)
        self.assertEqual(recovered.error, before.error)
        self.assertEqual(recovered.result, before.result)
        evidence = "Fixture execution ended; it performed no external operations"
        after = await self.rt.tasks.reconcile(before.id, {"resolution": "FAILED", "evidence": evidence,
            "confirmed_stopped": True, "external_actions_confirmed": True})
        self.assertEqual(after.state, "FAILED")
        self.assertEqual(after.result, before.result)
        self.assertEqual(after.error, before.error)
        self.assertEqual(after.runs[0].error["code"], before.runs[0].error["code"])
        self.assertEqual(after.runs[0].error["evidence"], evidence)
        self.assertFalse(self.rt.uncertain_slots)
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state", adapters={"worker": self.worker}).__aenter__()
        saved = await self.rt.tasks.get(before.id)
        self.assertEqual(saved.result, before.result)
        self.assertEqual(saved.error, before.error)

    async def test_successful_reconciliation_still_checks_delivery(self):
        before = await self.uncertain_partial()
        resolution = {"resolution": "SUCCEEDED", "evidence": "Fixture execution and effects were inspected",
            "confirmed_stopped": True, "external_actions_confirmed": True, "result": {"output": {}}}
        with self.assertRaises(RuntimeFault) as failure:
            await self.rt.tasks.reconcile(before.id, resolution)
        self.assertEqual(failure.exception.code, "EMPTY_OUTPUT")
        unchanged = await self.rt.tasks.get(before.id)
        self.assertEqual(unchanged.state, "UNCERTAIN")
        self.assertEqual(unchanged.result, before.result)
        self.assertTrue(self.rt.uncertain_slots)
        resolution["result"] = {"output": {"text": "Verified result"}}
        after = await self.rt.tasks.reconcile(before.id, resolution)
        self.assertEqual(after.state, "SUCCEEDED")
        self.assertEqual(after.result.metadata["acceptance"]["state"], "PASSED")
        self.assertEqual(after.runs[0].error["code"], before.runs[0].error["code"])
        self.assertFalse(self.rt.uncertain_slots)

    async def test_workflow_failure_blocks_downstream_and_keeps_result_requirements(self):
        from ma_runtime import WorkflowBuilder
        graph = (WorkflowBuilder().node("start", "start")
            .node("worker", "agent", agent_id="worker", payload={}, result_policy={"kind": "text"})
            .node("end", "end", outputs={}).connect("start", "worker").connect("worker", "end").build())
        definition = await self.rt.workflows.create("default", "Delivery checks", "definition", graph=graph)
        run = await self.rt.workflows.run(definition.id, {}, "run")
        with self.assertRaises(RuntimeFault):
            await self.rt.workflows.result(run.id, timeout=5)
        state = await self.rt.workflows.get_run(run.id)
        worker = next(node for node in state.nodes if node.node_id == "worker")
        self.assertEqual((await self.rt.tasks.get(worker.task_id)).error["code"], "EMPTY_OUTPUT")
        self.assertEqual(next(node for node in state.nodes if node.node_id == "end").state, "SKIPPED")

    def test_default_policy_preserves_existing_request_keys_and_rejects_remote_schema_refs(self):
        spec = TaskSpec(session_id="session", thread_id="thread", agent_id="worker", idempotency_key="key")
        old = spec.model_dump(mode="json", exclude={"result_policy", "tool_policy", "skill_policy", "context_budget_tokens", "context_refs", "continuation_policy"})
        self.assertEqual(request_hash(spec), content_hash(old))
        for schema in [{"$ref": "https://example.com/schema"}, {"type": "unknown"}]:
            with self.assertRaises(ValueError):
                ResultPolicy(output_schema=schema)
