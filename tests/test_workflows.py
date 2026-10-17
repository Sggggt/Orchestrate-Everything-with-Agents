"""Workflow contracts on a real SQLite store and scheduler; no external models."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import apsw
import httpx

from ma_runtime import (
    AdapterEvent, AgentConfig, AgentDescriptor, PlatformClient, Runtime, RuntimeConfig,
    RuntimeFault, TaskResult, WorkflowBuilder, WorkflowGraph,
)
from ma_runtime.bridges.compact import CompactBridge
from ma_runtime.bridges.dispatch import dispatch
from ma_runtime.models import json_dump
from ma_runtime.store import one
from ma_runtime.web.app import create_app
from ma_runtime.workflows import analyse_graph, condition, resolve


def linear(agent=None, payload=None, outputs=None):
    builder = WorkflowBuilder().node("start", "start")
    if agent:
        builder.node("worker", "agent", agent_id=agent,
                     payload=payload if payload is not None else {"input": "{{inputs.input}}", "_mock": {"delay": 0}})
        builder.connect("start", "worker")
    builder.node("end", "end", outputs=outputs if outputs is not None else {"text": "{{nodes.worker.text}}"} if agent else {"data": "{{inputs}}"})
    return builder.connect("worker" if agent else "start", "end").build()


class GraphTests(unittest.TestCase):
    def test_variable_resolution_preserves_types_and_handles_explicit_defaults(self):
        scope = {"inputs": {"number": 7, "flag": False, "items": ["first"]}, "nodes": {}}
        value = resolve({"n": "{{inputs.number}}", "flag": {"$ref": "inputs.flag"},
                         "fallback": {"$ref": "nodes.skipped.text", "default": None},
                         "text": "value={{inputs.number}}", "first": "{{inputs.items.0}}"}, scope)
        self.assertEqual(value, {"n": 7, "flag": False, "fallback": None, "text": "value=7", "first": "first"})
        with self.assertRaises(RuntimeFault):
            resolve("{{inputs.missing}}", scope)

    def test_cycle_handles_and_downstream_references_are_rejected(self):
        graph = linear().model_dump(mode="json")
        graph["edges"].append({"id": "cycle", "source": "end", "target": "start"})
        codes = {error["code"] for error in analyse_graph(WorkflowGraph.model_validate(graph))["errors"]}
        self.assertTrue({"DEPENDENCY_CYCLE", "INVALID_CONNECTION"} <= codes)
        graph = linear().model_dump(mode="json")
        graph["nodes"][-1]["config"] = {"outputs": {"value": "{{nodes.end.value}}"}}
        self.assertIn("VARIABLE_NOT_UPSTREAM", {error["code"] for error in analyse_graph(WorkflowGraph.model_validate(graph))["errors"]})
        graph["edges"][0]["source_handle"] = "true"
        self.assertIn("INVALID_HANDLE", {error["code"] for error in analyse_graph(WorkflowGraph.model_validate(graph))["errors"]})

    def test_input_schema_cannot_fetch_remote_references(self):
        graph = linear().model_dump(mode="json")
        graph["nodes"][0]["config"] = {"input_schema": {"type": "object", "properties": {"data": {"$ref": "https://example.invalid/schema"}}}}
        self.assertIn("INVALID_INPUT_SCHEMA", {error["code"] for error in analyse_graph(WorkflowGraph.model_validate(graph))["errors"]})

    def test_numeric_equality_unary_conditions_and_unicode_fields(self):
        self.assertTrue(condition({"left": 1, "right": 1.0, "operator": "equals"}, {}))
        self.assertFalse(condition({"left": True, "right": 1, "operator": "equals"}, {}))
        self.assertTrue(condition({"left": "", "right": "{{inputs.missing}}", "operator": "is_empty"}, {}))
        self.assertEqual(resolve("{{inputs.工作目标}}", {"inputs": {"工作目标": "核验"}}), "核验")


class CountingAgent:
    def __init__(self, identity):
        self.identity, self.active, self.maximum, self.inputs = identity, 0, 0, []

    def describe(self):
        return AgentDescriptor(agent_id=self.identity, max_concurrency=4)

    async def execute(self, request, context):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.inputs.append(request.payload)
        try:
            await asyncio.sleep(0.08)
            yield AdapterEvent(type="completed", result=TaskResult(output=request.payload))
        finally:
            self.active -= 1

    async def aclose(self):
        pass


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ma-workflows-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root / "state", RuntimeConfig(max_concurrency=2, reserved_external_slots=0)).__aenter__()
        self.files = self.root / "files"
        self.files.mkdir()
        await self.rt.projects.update("default", local_folder=str(self.files))
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock", project_id="default"))

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def workflow(self, graph=None, key="create"):
        return await self.rt.workflows.create("default", "测试工作流", key, graph=graph)

    async def wait_state(self, identity, states):
        for _ in range(140):
            run = await self.rt.workflows.get_run(identity)
            if run.state in states:
                return run
            await asyncio.sleep(0.025)
        self.fail(f"Run did not reach {states}: {run.state}")

    async def test_draft_validation_and_input_errors_do_not_start_partial_runs(self):
        draft = await self.workflow(WorkflowGraph())
        self.assertFalse(draft.validation["valid"])
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.run(draft.id, {}, "invalid")
        self.assertEqual(error.exception.code, "INVALID_WORKFLOW")
        graph = WorkflowBuilder().node("start", "start", input_schema={"type": "object", "required": ["number"], "properties": {"number": {"type": "integer"}}}).node("end", "end", outputs={}).connect("start", "end").build()
        saved = await self.workflow(graph, "typed")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.run(saved.id, {"number": "bad"}, "bad-input")
        self.assertEqual(error.exception.code, "INVALID_INPUT")
        self.assertEqual(len((await self.rt.sessions.list()).items), 0)

    async def test_single_project_and_database_membership_constraints(self):
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.create(None, "Missing project", "missing-project")
        self.assertEqual(error.exception.code, "PROJECT_REQUIRED")
        other = await self.rt.projects.create("Other", str(self.files), "other")
        await self.rt.agents.configure(AgentConfig(id="foreign", name="Foreign", kind="mock", project_id=other.id))
        with self.assertRaises(RuntimeFault) as error:
            await self.workflow(linear("foreign"))
        self.assertEqual(error.exception.code, "PROJECT_MISMATCH")
        saved = await self.workflow(linear("worker"))
        with self.assertRaises(RuntimeFault):
            await dispatch(self.rt, "workflows.update", {"workflow_id": saved.id, "expected_revision": 1, "project_id": other.id})
        with self.assertRaises(apsw.ConstraintError):
            await self.rt.store.call(lambda db: db.execute("UPDATE workflows SET project_id=? WHERE id=?", (other.id, saved.id)), transaction=True)
        run = await self.rt.workflows.run(saved.id, {"input": "hello"}, "run")
        await self.rt.workflows.result(run.id, timeout=3)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.projects.adopt(other.id, [run.session_id], ["worker"])
        self.assertEqual(error.exception.code, "PROJECT_CONFLICT")
        with self.assertRaises(apsw.ConstraintError):
            await self.rt.store.call(lambda db: db.execute("UPDATE sessions SET project_id=? WHERE id=?", (other.id, run.session_id)), transaction=True)
        self.assertEqual((await self.rt.workflows.get(saved.id)).project_id, "default")
        self.assertEqual(len((await self.rt.workflows.list(other.id)).items), 0)

    async def test_optimistic_save_and_immutable_running_snapshot(self):
        saved = await self.workflow(linear("worker", {"input": "old", "_mock": {"delay": 0.01}}))
        run = await self.rt.workflows.run(saved.id, {}, "old", expected_revision=1)
        revised = await self.rt.workflows.update(saved.id, 1, graph=linear(outputs={"text": "new"}))
        self.assertEqual(revised.revision, 2)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.update(saved.id, 1, name="lost edit")
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        result = await self.rt.workflows.result(run.id, timeout=3)
        self.assertEqual(result["outputs"]["text"], "worker: old")
        self.assertEqual((await self.rt.workflows.get_run(run.id)).revision, 1)
        newer = await self.rt.workflows.run(saved.id, {}, "new", expected_revision=2)
        self.assertEqual((await self.rt.workflows.result(newer.id, timeout=3))["outputs"]["text"], "new")

    async def test_run_idempotency_survives_definition_changes_and_archive(self):
        saved = await self.workflow(linear())
        first = await self.rt.workflows.run(saved.id, {"value": 5}, "once")
        await self.rt.workflows.result(first.id, timeout=3)
        await self.rt.workflows.update(saved.id, 1, graph=linear(outputs={"different": True}))
        await self.rt.workflows.archive(saved.id)
        again = await self.rt.workflows.run(saved.id, {"value": 5}, "once")
        self.assertEqual(first.id, again.id)
        self.assertTrue(again.reused)
        self.assertEqual(again.revision, 1)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.run(saved.id, {"value": 6}, "once")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len((await self.rt.workflows.runs(saved.id)).items), 1)
        with self.assertRaises(RuntimeFault):
            await self.rt.workflows.run(saved.id, {}, "new")
        self.assertEqual((await self.rt.workflows.list("default")).items, [])
        self.assertEqual(len((await self.rt.workflows.list("default", include_archived=True)).items), 1)
        await self.rt.workflows.archive(saved.id, False)
        self.assertEqual((await self.rt.workflows.get(saved.id)).state, "ACTIVE")

    async def test_create_receipts_and_lightweight_project_pagination(self):
        first = await self.workflow(linear(), "one")
        again = await self.workflow(linear(), "one")
        self.assertEqual(first.id, again.id)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.create("default", "different", "one", graph=linear())
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        second = await self.workflow(linear(), "two")
        page = await self.rt.workflows.list("default", limit=1)
        self.assertTrue(page.has_more)
        self.assertEqual(page.items[0].id, first.id)
        self.assertEqual(page.items[0].node_count, 2)
        self.assertNotIn("graph", page.items[0].model_dump())
        rest = await self.rt.workflows.list("default", cursor=page.cursor, limit=1)
        self.assertFalse(rest.has_more)
        self.assertEqual(rest.items[0].id, second.id)

    async def test_confirmed_delete_retains_graph_and_history_and_restores(self):
        saved = await self.workflow(linear())
        run = await self.rt.workflows.run(saved.id, {"kept": True}, "keep-run")
        result = await self.rt.workflows.result(run.id, timeout=3)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.delete(saved.id)
        self.assertEqual(error.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual((await self.rt.workflows.get(saved.id)).state, "ACTIVE")
        self.assertTrue((await self.rt.workflows.delete(saved.id, confirmed=True))["recoverable"])
        self.assertTrue((await self.rt.workflows.delete(saved.id, confirmed=True))["reused"])
        deleted = await self.rt.workflows.get(saved.id)
        self.assertEqual(deleted.state, "DELETED")
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(deleted.graph, saved.graph)
        self.assertEqual((await self.rt.workflows.list("default", include_archived=True)).items, [])
        trash = await self.rt.workflows.list("default", deleted_only=True)
        self.assertEqual([item.id for item in trash.items], [saved.id])
        self.assertEqual(trash.items[0].state, "DELETED")
        self.assertEqual(await self.rt.workflows.result(run.id), result)
        self.assertTrue((await self.rt.workflows.run(saved.id, {"kept": True}, "keep-run")).reused)
        for operation in (lambda: self.rt.workflows.run(saved.id, {}, "new"),
                          lambda: self.rt.workflows.update(saved.id, saved.revision, name="changed"),
                          lambda: self.rt.workflows.archive(saved.id)):
            with self.assertRaises(RuntimeFault) as error:
                await operation()
            self.assertEqual(error.exception.code, "WORKFLOW_DELETED")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        self.assertEqual((await self.rt.workflows.get(saved.id)).state, "DELETED")
        restored = await self.rt.workflows.restore(saved.id)
        self.assertEqual((restored.id, restored.project_id, restored.state), (saved.id, "default", "ACTIVE"))
        self.assertEqual(restored.graph, saved.graph)
        self.assertEqual((await self.rt.workflows.restore(saved.id)).revision, saved.revision)
        self.assertEqual((await self.rt.workflows.list("default", deleted_only=True)).items, [])
        self.assertEqual(await self.rt.workflows.result(run.id), result)

    async def test_delete_blocks_unfinished_runs_and_restores_archived_state(self):
        saved = await self.workflow(linear("worker", {"input": "wait", "_mock": {"delay": 0, "wait_for_input": True}}))
        run = await self.rt.workflows.run(saved.id, {}, "waiting")
        await self.wait_state(run.id, {"WAITING"})
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.delete(saved.id, confirmed=True)
        self.assertEqual(error.exception.code, "RESOURCE_BUSY")
        self.assertEqual((await self.rt.workflows.list("default", deleted_only=True)).items, [])
        await self.rt.workflows.cancel(run.id)
        await self.wait_state(run.id, {"CANCELLED"})
        await self.rt.workflows.archive(saved.id)
        await self.rt.workflows.delete(saved.id, confirmed=True)
        self.assertEqual((await self.rt.workflows.restore(saved.id)).state, "ARCHIVED")
        await self.rt.workflows.delete(saved.id, confirmed=True)
        await self.rt.projects.delete("default", confirmed=True)
        with self.assertRaises(RuntimeFault):
            await self.rt.workflows.restore(saved.id)
        await self.rt.projects.restore("default")
        self.assertEqual((await self.rt.workflows.get(saved.id)).state, "DELETED")
        self.assertEqual((await self.rt.workflows.restore(saved.id)).state, "ARCHIVED")

    async def test_branch_skip_and_join_with_typed_fallback_values(self):
        graph = (WorkflowBuilder().node("start", "start")
                 .node("choose", "condition", left="{{inputs.number}}", operator="gt", right=0)
                 .node("positive", "template", template="positive {{inputs.number}}")
                 .node("negative", "template", template="negative {{inputs.number}}")
                 .node("end", "end", outputs={"positive": {"$ref": "nodes.positive.text", "default": None},
                                              "negative": {"$ref": "nodes.negative.text", "default": None}})
                 .connect("start", "choose").connect("choose", "positive", branch="true")
                 .connect("choose", "negative", branch="false").connect("positive", "end")
                 .connect("negative", "end").build())
        saved = await self.workflow(graph)
        for number, expected, skipped in [(2, "positive", "negative"), (-2, "negative", "positive")]:
            run = await self.rt.workflows.run(saved.id, {"number": number}, str(number))
            result = await self.rt.workflows.result(run.id, timeout=3)
            self.assertEqual(result["outputs"][expected], f"{expected} {number}")
            self.assertIsNone(result["outputs"][skipped])
            states = {node.node_id: node.state for node in (await self.rt.workflows.get_run(run.id)).nodes}
            self.assertEqual(states[skipped], "SKIPPED")
            self.assertEqual(states["end"], "SUCCEEDED")

    async def test_fanout_uses_existing_budget_and_isolated_threads(self):
        counter = CountingAgent("counter")
        await self.rt.agents.configure(AgentConfig(id="counter", name="Counter", kind="mock", max_concurrency=4))
        self.rt.adapters["counter"] = counter
        builder = WorkflowBuilder().node("start", "start")
        for number in range(3):
            builder.node(f"node{number}", "agent", agent_id="counter", payload={"number": "{{inputs.number}}", "flag": "{{inputs.flag}}"}).connect("start", f"node{number}")
        builder.node("end", "end", outputs={"number": "{{nodes.node0.number}}", "flag": "{{nodes.node2.flag}}"})
        for number in range(3):
            builder.connect(f"node{number}", "end")
        saved = await self.workflow(builder.build())
        run = await self.rt.workflows.run(saved.id, {"number": 7, "flag": False}, "parallel")
        self.assertEqual((await self.rt.workflows.result(run.id, timeout=3))["outputs"], {"number": 7, "flag": False})
        self.assertEqual(counter.maximum, 2)
        self.assertEqual(len(counter.inputs), 3)
        snapshot = await self.rt.sessions.snapshot(run.session_id)
        self.assertEqual(len({task.thread_id for task in snapshot.tasks}), 3)

    async def test_wait_resume_keeps_node_task_and_completed_prefix(self):
        saved = await self.workflow(linear("worker", {"input": "wait", "_mock": {"delay": 0, "wait_for_input": True}}))
        run = await self.rt.workflows.run(saved.id, {}, "wait")
        waiting = await self.wait_state(run.id, {"WAITING"})
        task = next(node for node in waiting.nodes if node.node_id == "worker").task_id
        wait = (await self.rt.tasks.get(task)).wait
        await self.rt.workflows.resume_node(run.id, "worker", {"answer": "continue", "wait_token": wait["wait_token"]}, "resume")
        self.assertIn("已恢复等待任务", (await self.rt.workflows.result(run.id, timeout=3))["outputs"]["text"])
        done = await self.rt.workflows.get_run(run.id)
        self.assertEqual(next(node for node in done.nodes if node.node_id == "worker").task_id, task)
        self.assertEqual(len((await self.rt.tasks.get(task)).runs), 2)

    async def test_cancel_waiting_flow_and_project_lifecycle_guards(self):
        saved = await self.workflow(linear("worker", {"input": "wait", "_mock": {"delay": 0, "wait_for_input": True}}))
        run = await self.rt.workflows.run(saved.id, {}, "wait")
        await self.wait_state(run.id, {"WAITING"})
        for operation in (lambda: self.rt.projects.archive("default"),
                          lambda: self.rt.projects.delete("default", confirmed=True),
                          lambda: self.rt.sessions.delete(run.session_id, confirmed=True),
                          lambda: self.rt.sessions.close(run.session_id)):
            with self.assertRaises(RuntimeFault) as error:
                await operation()
            self.assertEqual(error.exception.code, "RESOURCE_BUSY")
        receipt = await self.rt.workflows.cancel(run.id)
        self.assertFalse(receipt["confirmed_stopped"])
        cancelled = await self.wait_state(run.id, {"CANCELLED"})
        self.assertEqual({node.node_id: node.state for node in cancelled.nodes}["worker"], "CANCELLED")
        self.assertEqual({node.node_id: node.state for node in cancelled.nodes}["end"], "SKIPPED")
        self.assertTrue((await self.rt.workflows.cancel(run.id))["confirmed_stopped"])
        await self.rt.projects.archive("default")

    async def test_cancel_running_agent_waits_for_actual_task_stop(self):
        saved = await self.workflow(linear("worker", {"input": "running" * 20, "_mock": {"delay": 0.02}}))
        run = await self.rt.workflows.run(saved.id, {}, "running")
        for _ in range(80):
            if self.rt.active:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(self.rt.active)
        await self.rt.workflows.cancel(run.id)
        final = await self.wait_state(run.id, {"CANCELLED"})
        self.assertEqual(next(node for node in final.nodes if node.node_id == "worker").state, "CANCELLED")
        self.assertEqual(next(node for node in final.nodes if node.node_id == "end").state, "SKIPPED")
        self.assertFalse(self.rt.active)

    async def test_cancel_stops_queued_receipts_in_the_acceptance_transaction(self):
        self.rt.scheduler.cancel()
        await self.rt.scheduler
        saved = await self.workflow(linear("worker"))
        run = await self.rt.workflows.run(saved.id, {"input": "do not dispatch"}, "queued-cancel")
        await self.rt.workflows.tick()
        worker = next(node for node in (await self.rt.workflows.get_run(run.id)).nodes if node.node_id == "worker")
        self.assertEqual((await self.rt.tasks.get(worker.task_id)).state, "QUEUED")
        await self.rt.workflows.cancel(run.id)
        self.assertEqual((await self.rt.tasks.get(worker.task_id)).state, "CANCELLED")
        await self.rt.workflows.tick()
        self.assertEqual((await self.rt.workflows.get_run(run.id)).state, "CANCELLED")

    async def test_failed_parallel_node_settles_other_tasks_before_final_failure(self):
        graph = (WorkflowBuilder().node("start", "start")
                 .node("worker", "agent", agent_id="worker", payload={"input": "parallel", "_mock": {"delay": 0.1}})
                 .node("broken", "template", template="{{inputs.missing}}")
                 .node("end", "end", outputs={})
                 .connect("start", "worker").connect("start", "broken")
                 .connect("worker", "end").connect("broken", "end").build())
        saved = await self.workflow(graph)
        run = await self.rt.workflows.run(saved.id, {}, "failure")
        failed = await self.wait_state(run.id, {"FAILED"})
        self.assertTrue(all(node.state in {"SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"} for node in failed.nodes))
        self.assertEqual(next(node for node in failed.nodes if node.node_id == "worker").state, "CANCELLED")
        self.assertFalse(self.rt.active)

    async def test_summaries_keep_full_peer_results_out_of_markdown_projection(self):
        saved = await self.workflow(linear("worker", {"input": "摘要\n\n<!-- agent:details -->\nPEER_ONLY", "_mock": {"delay": 0}}))
        run = await self.rt.workflows.run(saved.id, {}, "summary")
        await self.rt.workflows.result(run.id, timeout=3)
        result = await self.rt.workflows.get_run(run.id)
        node = next(node for node in result.nodes if node.node_id == "worker")
        self.assertIn("PEER_ONLY", node.output["text"])
        self.assertNotIn("PEER_ONLY", node.summary_markdown)
        self.assertNotIn("PEER_ONLY", result.summary_markdown)

    async def test_recovery_observes_durable_task_receipts_without_duplicates(self):
        # Stop only this disposable scheduler so the crash boundary is reproducible.
        self.rt.scheduler.cancel()
        await self.rt.scheduler
        saved = await self.workflow(linear("worker"))
        run = await self.rt.workflows.run(saved.id, {"input": "restart"}, "run")
        await self.rt.workflows.tick()
        node = next(node for node in (await self.rt.workflows.get_run(run.id)).nodes if node.node_id == "worker")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state", RuntimeConfig(reserved_external_slots=0)).__aenter__()
        self.assertEqual((await self.rt.workflows.result(run.id, timeout=3))["outputs"]["text"], "worker: restart")
        self.assertEqual(len((await self.rt.sessions.snapshot(run.session_id)).tasks), 1)
        self.assertEqual(next(item for item in (await self.rt.workflows.get_run(run.id)).nodes if item.node_id == "worker").task_id, node.task_id)
        self.assertTrue((self.root / "state/backups/before-schema-5.sqlite").exists())

    async def test_uncertain_node_blocks_downstream_and_cancel_does_not_invent_stop(self):
        self.rt.scheduler.cancel()
        await self.rt.scheduler
        saved = await self.workflow(linear("worker"))
        run = await self.rt.workflows.run(saved.id, {"input": "uncertain"}, "run")
        await self.rt.workflows.tick()
        node = next(node for node in (await self.rt.workflows.get_run(run.id)).nodes if node.node_id == "worker")
        await self.rt.store.call(lambda db: db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=?", (node.task_id,)), transaction=True)
        await self.rt.workflows.tick()
        uncertain = await self.rt.workflows.get_run(run.id)
        self.assertEqual(uncertain.state, "UNCERTAIN")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.delete(saved.id, confirmed=True)
        self.assertEqual(error.exception.code, "RESOURCE_BUSY")
        self.assertEqual(next(item for item in uncertain.nodes if item.node_id == "end").state, "PENDING")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.result(run.id)
        self.assertEqual(error.exception.code, "OUTCOME_UNCERTAIN")
        await self.rt.workflows.cancel(run.id)
        await self.rt.workflows.tick()
        self.assertEqual((await self.rt.workflows.get_run(run.id)).state, "UNCERTAIN")
        # Synthetic reconciliation evidence in this fixture; production uses tasks.reconcile.
        await self.rt.store.call(lambda db: db.execute("UPDATE tasks SET state='CANCELLED' WHERE id=?", (node.task_id,)), transaction=True)
        await self.rt.workflows.tick()
        self.assertEqual((await self.rt.workflows.get_run(run.id)).state, "CANCELLED")

    async def test_bad_variable_fails_one_run_and_keeps_scheduler_healthy(self):
        graph = linear(outputs={"bad": "{{inputs.missing}}"})
        saved = await self.workflow(graph)
        run = await self.rt.workflows.run(saved.id, {}, "bad")
        failed = await self.wait_state(run.id, {"FAILED"})
        self.assertEqual(failed.error["code"], "VARIABLE_NOT_FOUND")
        healthy = await self.workflow(linear(), "healthy")
        next_run = await self.rt.workflows.run(healthy.id, {"ok": True}, "good")
        self.assertEqual((await self.rt.workflows.result(next_run.id, timeout=3))["outputs"], {"data": {"ok": True}})
        self.assertTrue(self.rt.accepting)

    async def test_http_sdk_and_compact_mcp_cover_workflow_capabilities(self):
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                saved = await client.workflows.create("default", "SDK canvas", "sdk", graph=linear())
                guide = await client.protocol()
                self.assertIn("workflows.run", guide["methods"])
                self.assertIn("WorkflowGraph", guide["schemas"])
                run = await client.workflows.run(saved["id"], {"via": "sdk"}, "sdk-run")
                self.assertEqual((await client.workflows.result(run["id"], timeout=3))["outputs"], {"data": {"via": "sdk"}})
                self.assertEqual(len(await client.workflows.node_types()), 6)
                await client.workflows.delete(saved["id"], confirmed=True)
                self.assertEqual((await client.workflows.list("default", deleted_only=True))["items"][0]["id"], saved["id"])
                self.assertEqual((await client.workflows.restore(saved["id"]))["state"], "ACTIVE")
            async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=app),
                                         headers={"Authorization": "Bearer " + app.state.token}) as http:
                response = await http.get("/api/workflows", params={"project_id": "default"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["items"]), 1)
                response = await http.patch("/api/workflows/" + saved["id"], json={"expected_revision": 0, "name": "conflict"})
                self.assertEqual(response.status_code, 409)
                response = await http.request("DELETE", "/api/workflows/" + saved["id"], json={"confirmed": False})
                self.assertEqual(response.status_code, 400)
                response = await http.request("DELETE", "/api/workflows/" + saved["id"], json={"confirmed": True})
                self.assertTrue(response.json()["deleted"])
                response = await http.post("/api/workflows/" + saved["id"] + "/restore", json={})
                self.assertEqual(response.json()["state"], "ACTIVE")
        async def invoke(method, params):
            return json.loads(json_dump(await dispatch(self.rt, method, params)))
        bridge = CompactBridge(invoke)
        contract = bridge.guide("workflows", "create")
        self.assertEqual(set(contract["schemas"]), {"WorkflowGraph"})
        result = await bridge.call("workflows", "validate", {"project_id": "default", "graph": linear().model_dump(mode="json")})
        self.assertTrue(result["result"]["valid"])
        self.assertIn("workflow_graph", bridge.guide("development")["schemas"])
        deleted = await bridge.call("workflows", "delete", {"workflow_id": saved["id"], "confirmed": True})
        self.assertTrue(deleted["result"]["deleted"])
        restored = await bridge.call("workflows", "restore", {"workflow_id": saved["id"]})
        self.assertEqual(restored["result"]["state"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
