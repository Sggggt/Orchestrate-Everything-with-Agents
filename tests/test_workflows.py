"""Business topology executes through native LangGraph and real OEA receipts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oea import AgentDefinition, Completion, Model, WorkflowBuilder
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows


class EchoModel:
    """Protocol fixture; graph, persistence and tool boundaries remain real."""
    def __init__(self, count=0):
        self.count, self.active, self.peak, self.calls = count, 0, 0, 0
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        if not count:
            self.release.set()

    async def complete(self, model, messages, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == self.count:
            self.ready.set()
        try:
            await self.release.wait()
            text = next(message["content"] for message in reversed(messages) if message["role"] == "user")
            return Completion(text=str(text), usage={"prompt_tokens": 30, "completion_tokens": 2})
        finally:
            self.active -= 1


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-workflow-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state", model_api=EchoModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://example.invalid/v1", model="protocol-fixture")))

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def run_graph(self, builder, inputs=None, **options):
        definition = await self.host.workflows.create("p", "Work", builder.build(**options))
        run = await self.host.workflows.run(definition["id"], inputs or {}, "intent", project_id="p")
        return run

    async def until(self, identity, state, *, kind=None):
        async with asyncio.timeout(15):
            while True:
                changed = self.host.workflows.signal(identity)
                value = await self.host.workflows.get_run(identity, project_id="p")
                if value["state"] == state and (kind is None or any(item["kind"] == kind for item in (value["wait"] or {}).get("interrupts", ()))):
                    return value
                if value["state"] in {"failed", "cancelled", "needs_review"} and value["state"] != state and not (state == "cancelled" and value["state"] == "needs_review"):
                    self.fail(str(value))
                await changed.wait()

    async def test_native_loop_condition_and_function(self):
        calls = []
        def increment(value: int) -> int:
            calls.append(value)
            return value + 1
        self.host.workflows.register_function(increment)
        graph = (WorkflowBuilder().node("start", "start")
                 .node("increment", "function", name="increment", arguments={"value": {"$ref": "nodes.increment.value", "default": 0}}, join="any")
                 .node("condition", "condition", left="{{nodes.increment.value}}", operator="lt", right=3)
                 .node("end", "end", outputs={"answer": "{{nodes.increment.value}}"})
                 .connect("start", "increment").connect("increment", "condition")
                 .connect("condition", "increment", branch="true").connect("condition", "end", branch="false"))
        run = await self.run_graph(graph)
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=15), {"answer": 3})
        self.assertEqual(calls, [0, 1, 2])
        snapshot = await self.host.workflows._compile(graph.build().model_dump_json()).aget_state(self.host.workflows.config(run["id"], graph.build()))
        self.assertEqual(snapshot.values["iterations"]["increment"], 3)
        self.assertNotIn("arguments", snapshot.values)

    async def test_parallel_functions_and_all_join(self):
        ready, release = asyncio.Event(), asyncio.Event()
        active = 0
        async def branch(value: int) -> int:
            nonlocal active
            active += 1
            if active == 2:
                ready.set()
            await release.wait()
            return value
        self.host.workflows.register_function(branch)
        graph = (WorkflowBuilder().node("start", "start").node("left", "function", name="branch", arguments={"value": 11})
                 .node("right", "function", name="branch", arguments={"value": 17})
                 .node("end", "end", outputs={"left": "{{nodes.left.value}}", "right": "{{nodes.right.value}}"})
                 .connect("start", "left").connect("start", "right").connect("left", "end").connect("right", "end"))
        run = await self.run_graph(graph)
        await asyncio.wait_for(ready.wait(), 10)
        release.set()
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=15), {"left": 11, "right": 17})

    async def test_native_map_twenty_actual_agent_runs_and_common_budget(self):
        model = self.host.model_api = EchoModel(20)
        graph = (WorkflowBuilder().node("start", "start").node("map", "map", title="Review item", agent_id="worker", item_label="Item {{index}}", input={"index": "{{index}}", "value": "{{item}}"})
                 .node("end", "end", outputs={"batch": "{{nodes.map.outputs}}"}).connect("start", "map").connect("map", "end"))
        run = await self.run_graph(graph, {"items": list(range(20))})
        await asyncio.wait_for(model.ready.wait(), 15)
        self.assertEqual(model.peak, 20)
        members = await self.host.store.call(lambda db: rows(db, "SELECT r.*,i.name FROM runs r JOIN instances i ON i.id=r.instance_id WHERE workflow_run_id=?", (run["id"],)))
        self.assertEqual(len({item["instance_id"] for item in members}), 20)
        self.assertEqual({json.loads(item["display_json"])["item_label"] for item in members}, {"Item " + str(index) for index in range(20)})
        self.assertTrue(all(json.loads(item["display_json"])["title"] == "Review item" for item in members))
        self.assertTrue(all(item["root_workflow_run_id"] == run["id"] and item["root_run_id"] is None and item["budget_id"] == run["budget_id"] for item in members))
        metadata = await self.host.workflows.node_result(run["id"], "map", project_id="p", metadata_only=True)
        self.assertEqual(metadata["reference"], {"kind": "map", "count": 20})
        page = await self.host.workflows.members(run["id"], "map", project_id="p", limit=7)
        self.assertEqual([item["ordinal"] for item in page["items"]], list(range(7)))
        self.assertEqual(page["cursor"], 7)
        self.assertTrue(all("output" not in item for item in page["items"]))
        with self.assertRaises(RuntimeFault) as foreign:
            await self.host.workflows.members(run["id"], "map", project_id="other")
        self.assertEqual(foreign.exception.code, "PERMISSION_DENIED")
        model.release.set()
        output = await self.host.workflows.result(run["id"], project_id="p", timeout=20)
        self.assertEqual(len(output["batch"]), 20)
        self.assertEqual([json.loads(item["text"])["index"] for item in output["batch"]], list(range(20)))
        budget = await self.host.budgets.get(run["budget_id"])
        self.assertEqual(budget["usage"]["model_calls"], 20)

    async def test_wait_reply_once_and_before_interrupt_function_not_repeated(self):
        calls = []
        def first() -> int:
            calls.append(1)
            return 37
        self.host.workflows.register_function(first)
        graph = (WorkflowBuilder().node("start", "start").node("first", "function", name="first").node("wait", "wait", prompt="Confirm")
                 .node("end", "end", outputs={"first": "{{nodes.first.value}}", "reply": "{{nodes.wait}}"})
                 .connect("start", "first").connect("first", "wait").connect("wait", "end"))
        run = await self.run_graph(graph)
        await self.until(run["id"], "waiting", kind="input")
        await self.host.workflows.resume(run["id"], "reply", project_id="p", input={"confirm": True})
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"first": 37, "reply": {"confirm": True}})
        await self.host.workflows.resume(run["id"], "reply", project_id="p", input={"confirm": True})
        self.assertEqual(calls, [1])

    async def test_subgraph_result_scope_and_budget(self):
        child = (WorkflowBuilder().node("start", "start").node("end", "end", outputs={"value": "{{inputs.value}}"}).connect("start", "end"))
        saved = await self.host.workflows.create("p", "Child", child.build())
        parent = (WorkflowBuilder().node("start", "start").node("child", "subgraph", workflow_id=saved["id"], inputs={"value": 42})
                  .node("end", "end", outputs={"child": "{{nodes.child}}"}).connect("start", "child").connect("child", "end"))
        run = await self.run_graph(parent)
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"child": {"value": 42}})
        nested = await self.host.store.call(lambda db: one(db, "SELECT * FROM workflow_runs WHERE parent_workflow_run_id=?", (run["id"],)))
        self.assertEqual(nested["budget_id"], run["budget_id"])
        with self.assertRaises(RuntimeFault) as error:
            await self.host.workflows.get_run(run["id"], project_id="different")
        self.assertEqual(error.exception.code, "PERMISSION_DENIED")

    async def test_receipt_before_checkpoint_crash_reuses_computation(self):
        calls = []
        def compute() -> int:
            calls.append(1)
            return 42
        self.host.workflows.register_function(compute)
        # Cancel the graph consumer after the durable computation receipt,
        # leaving its checkpoint at the original node boundary.
        graph = (WorkflowBuilder().node("start", "start").node("compute", "function", name="compute")
                 .node("end", "end", outputs={"value": "{{nodes.compute.value}}"}).connect("start", "compute").connect("compute", "end"))
        saved = await self.host.workflows.create("p", "Crash", graph.build())
        original_compute = self.host.workflows.compute
        async def crash(*args):
            result = await original_compute(*args)
            raise RuntimeFault("SIMULATED_CRASH", "Checkpoint write was interrupted")
        with patch.object(self.host.workflows, "compute", crash):
            run = await self.host.workflows.run(saved["id"], {}, "intent", project_id="p")
            await self.until(run["id"], "failed")
        await self.host.workflows.resume(run["id"], "recover", project_id="p")
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"value": 42})
        self.assertEqual(calls, [1])

    async def test_accepted_agent_run_without_node_reference_is_reused_after_restart(self):
        from oea.orchestration.graphs.workflow_context import WorkflowContext
        class ProcessLoss(BaseException):
            pass
        crashed = asyncio.Event()
        graph = (WorkflowBuilder().node("start", "start").node("work", "agent", agent_id="worker", input="original input")
                 .node("end", "end", outputs={"text": "{{nodes.work.text}}"}).connect("start", "work").connect("work", "end"))
        original_save = WorkflowContext.save
        async def lose_reference(context, node, step, state, reference=None, error=None):
            if node.id == "work" and state == "waiting" and reference and reference.get("kind") == "run":
                crashed.set()
                # Process loss bypasses normal fail-fast handling. Child work
                # and the latest graph checkpoint remain independently durable.
                raise ProcessLoss()
            return await original_save(context, node, step, state, reference, error)
        model = self.host.model_api
        with patch.object(WorkflowContext, "save", lose_reference):
            run = await self.run_graph(graph)
            await asyncio.wait_for(crashed.wait(), 10)
            await asyncio.gather(*tuple(self.host.workflows.jobs.values()), return_exceptions=True)
        self.assertEqual((await self.host.workflows.get_run(run["id"], project_id="p"))["state"], "running")
        accepted = await self.host.store.call(lambda db: rows(db, "SELECT id,instance_id,budget_id FROM runs WHERE workflow_run_id=?", (run["id"],)))
        self.assertEqual(len(accepted), 1)
        await self.host.runs.result(accepted[0]["id"], timeout=10)
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=model).__aenter__()
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"text": "original input"})
        recovered = await self.host.store.call(lambda db: rows(db, "SELECT id,instance_id,budget_id FROM runs WHERE workflow_run_id=?", (run["id"],)))
        self.assertEqual(recovered, accepted)
        self.assertEqual(model.calls, 1)
        self.assertEqual((await self.host.budgets.get(run["budget_id"]))["usage"]["model_calls"], 1)

    async def test_cancel_owns_only_its_children_in_shared_session(self):
        self.host.model_api = EchoModel(2)
        session = await self.host.sessions.create("p")
        graph = (WorkflowBuilder().node("start", "start").node("work", "agent", agent_id="worker", input="work")
                 .node("end", "end", outputs={"text": "{{nodes.work.text}}"}).connect("start", "work").connect("work", "end"))
        saved = await self.host.workflows.create("p", "Scoped", graph.build())
        first = await self.host.workflows.run(saved["id"], {}, "first", project_id="p", session_id=session["id"])
        other = await self.host.workflows.run(saved["id"], {}, "other", project_id="p", session_id=session["id"])
        await asyncio.wait_for(self.host.model_api.ready.wait(), 15)
        cancelled = await self.host.workflows.cancel(first["id"], project_id="p")
        self.assertEqual(cancelled["state"], "cancelled")
        self.host.model_api.release.set()
        self.assertEqual(await self.host.workflows.result(other["id"], project_id="p", timeout=10), {"text": "work"})
        child_states = await self.host.store.call(lambda db: rows(db, "SELECT workflow_run_id,state FROM runs"))
        self.assertCountEqual(
            [(item["workflow_run_id"], item["state"]) for item in child_states],
            [(first["id"], "cancelled"), (other["id"], "completed")],
        )

    async def test_empty_map_and_frozen_revision_idempotency(self):
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker")
                 .node("end", "end", outputs={"values": "{{nodes.batch.outputs}}"}).connect("start", "batch").connect("batch", "end"))
        saved = await self.host.workflows.create("p", "Empty", graph.build())
        run = await self.host.workflows.run(saved["id"], {"items": []}, "intent", project_id="p")
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"values": []})
        await self.host.workflows.update(saved["id"], "p", 1, name="Renamed")
        replay = await self.host.workflows.run(saved["id"], {"items": []}, "intent", project_id="p")
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["id"], run["id"])
        with self.assertRaises(RuntimeFault):
            await self.host.workflows.update(saved["id"], "p", 1, name="Conflict")

    async def test_independent_evidence_survives_another_branch_failure(self):
        completed = []
        def fail() -> int:
            raise RuntimeFault("EXPECTED_FAILURE", "Failed branch")
        def succeed() -> int:
            completed.append(37)
            return 37
        self.host.workflows.register_function(fail)
        self.host.workflows.register_function(succeed)
        graph = (WorkflowBuilder().node("start", "start").node("bad", "function", name="fail").node("good", "function", name="succeed")
                 .node("end", "end", join="any", outputs={"good": "{{nodes.good.value}}", "bad": "{{nodes.bad}}"})
                 .connect("start", "bad").connect("start", "good").connect("bad", "end").connect("good", "end"))
        run = await self.run_graph(graph, failure_policy="collect_independent")
        await self.until(run["id"], "failed")
        self.assertEqual((await self.host.workflows.node_result(run["id"], "good", project_id="p"))["output"], {"value": 37})
        self.assertEqual(completed, [37])
        await self.host.workflows.resume(run["id"], "inspect-recovery", project_id="p")
        await self.until(run["id"], "failed")
        self.assertEqual(completed, [37])

    async def test_collect_repair_continues_from_native_anchor_and_keeps_old_evidence(self):
        fixed = False
        counts = {"good": 0, "bad": 0}
        def good() -> int:
            counts["good"] += 1
            return 11
        def bad() -> int:
            counts["bad"] += 1
            if not fixed:
                raise RuntimeFault("EXPECTED_FAILURE", "Original failure evidence")
            return 37
        self.host.workflows.register_function(good)
        self.host.workflows.register_function(bad)
        graph = (WorkflowBuilder().node("start", "start").node("good", "function", name="good").node("bad", "function", name="bad")
                 .node("end", "end", outputs={"good": "{{nodes.good.value}}", "bad": "{{nodes.bad.value}}"})
                 .connect("start", "good").connect("start", "bad").connect("good", "end").connect("bad", "end"))
        run = await self.run_graph(graph, failure_policy="collect_independent")
        await self.until(run["id"], "failed")
        original = await self.host.workflows.node_result(run["id"], "bad", project_id="p")
        fixed = True
        await self.host.workflows.resume(run["id"], "repair-bad", project_id="p", node_id="bad")
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=15), {"good": 11, "bad": 37})
        self.assertEqual(counts, {"good": 1, "bad": 2})
        current = await self.host.workflows.get_run(run["id"], project_id="p")
        self.assertEqual(current["budget_id"], run["budget_id"])
        retained = await self.host.workflows.node_result(run["id"], "bad", project_id="p", step_id=original["step_id"], version=original["version"])
        self.assertEqual(retained, original)
        self.assertEqual((await self.host.budgets.get(run["budget_id"]))["usage"]["model_calls"], 0)

    async def test_two_failed_branches_repair_separately_without_repeating_success(self):
        fixed, counts = set(), {"left": 0, "delay": 0, "right": 0}
        def left() -> int:
            counts["left"] += 1
            if "left" not in fixed:
                raise RuntimeFault("LEFT_FAILURE", "Left fault")
            return 11
        def delay() -> int:
            counts["delay"] += 1
            return 17
        def right(value: int) -> int:
            counts["right"] += 1
            if "right" not in fixed:
                raise RuntimeFault("RIGHT_FAILURE", "Right fault")
            return value
        for function in (left, delay, right):
            self.host.workflows.register_function(function)
        graph = (WorkflowBuilder().node("start", "start").node("left", "function", name="left").node("delay", "function", name="delay")
                 .node("right", "function", name="right", arguments={"value": "{{nodes.delay.value}}"})
                 .node("end", "end", outputs={"left": "{{nodes.left.value}}", "right": "{{nodes.right.value}}"})
                 .connect("start", "left").connect("start", "delay").connect("delay", "right").connect("left", "end").connect("right", "end"))
        run = await self.run_graph(graph, failure_policy="collect_independent")
        await self.until(run["id"], "failed")
        fixed.add("left")
        await self.host.workflows.resume(run["id"], "repair-left", project_id="p", node_id="left")
        await self.until(run["id"], "failed")
        self.assertEqual(counts, {"left": 2, "delay": 1, "right": 1})
        fixed.add("right")
        await self.host.workflows.resume(run["id"], "repair-right", project_id="p", node_id="right")
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=15), {"left": 11, "right": 17})
        self.assertEqual(counts, {"left": 2, "delay": 1, "right": 2})

    async def test_two_parallel_input_interrupts_can_resume_separately_after_restart(self):
        graph = (WorkflowBuilder().node("start", "start").node("left", "wait").node("right", "wait")
                 .node("end", "end", outputs={"left": "{{nodes.left}}", "right": "{{nodes.right}}"})
                 .connect("start", "left").connect("start", "right").connect("left", "end").connect("right", "end"))
        run = await self.run_graph(graph)
        waiting = await self.until(run["id"], "waiting", kind="input")
        ids = {item["node_id"]: item["id"] for item in waiting["wait"]["interrupts"]}
        self.assertEqual(set(ids), {"left", "right"})
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=EchoModel()).__aenter__()
        await self.until(run["id"], "waiting", kind="input")
        await self.host.workflows.resume(run["id"], "left-reply", project_id="p", input="L", interrupt_id=ids["left"])
        async with asyncio.timeout(10):
            while True:
                changed = self.host.workflows.signal(run["id"])
                value = await self.host.workflows.get_run(run["id"], project_id="p")
                if value["state"] == "waiting" and len((value["wait"] or {}).get("interrupts", ())) == 1:
                    break
                await changed.wait()
        await self.host.workflows.resume(run["id"], "right-reply", project_id="p", input="R", interrupt_id=ids["right"])
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"left": "L", "right": "R"})

    async def test_cannot_replace_persistent_saver_with_in_memory_graph(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from oea.orchestration.graphs import compile_agent_graph
        from oea.contracts.configuration import Execution
        self.host.register_graph("volatile", lambda saver: compile_agent_graph(InMemorySaver()), version="1")
        with self.assertRaises(RuntimeFault) as error:
            self.host.agent_graphs.resolve(Execution(graph="volatile"))
        self.assertEqual(error.exception.code, "PERSISTENT_GRAPH_REQUIRED")

    async def test_registered_code_graph_uses_oea_model_and_delivery(self):
        from langgraph.graph import START, END, StateGraph
        from oea.orchestration.graphs import AgentState, AgentGraphContext
        from oea.orchestration.graphs.default import prepare, model, deliver
        from oea.contracts.configuration import Execution
        from oea.contracts.domain import RunSpec
        calls = []
        def factory(saver):
            builder = StateGraph(AgentState, context_schema=AgentGraphContext)
            async def note(state: AgentState):
                calls.append(state["run_id"])
                return {}
            for name, handler in (("prepare", prepare), ("note", note), ("model", model), ("deliver", deliver)):
                builder.add_node(name, handler)
            builder.add_edge(START, "prepare").add_edge("prepare", "note").add_edge("note", "model").add_edge("model", "deliver").add_edge("deliver", END)
            return builder.compile(checkpointer=saver)
        self.host.register_graph("review", factory, version="a")
        definition = AgentDefinition.model_validate((await self.host.agents.get("worker"))["definition"])
        await self.host.agents.save(definition.model_copy(update={"execution": Execution(graph="review", graph_version="a")}))
        session = await self.host.sessions.create("p")
        instance = await self.host.instances.create(session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="code graph", idempotency_key="work"))
        result = await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(result.text, "code graph")
        self.assertEqual(calls, [run.id])
        topology = await self.host.registry.call("executions.graphs")
        self.assertFalse(next(item for item in topology if item["name"] == "review")["editable"])

    async def test_cancel_does_not_pretend_an_owned_function_has_stopped(self):
        started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def held() -> int:
            started.set()
            await release.wait()
            stopped.set()
            return 37
        self.host.workflows.register_function(held)
        graph = (WorkflowBuilder().node("start", "start").node("held", "function", name="held")
                 .node("end", "end", outputs={"value": "{{nodes.held.value}}"}).connect("start", "held").connect("held", "end"))
        run = await self.run_graph(graph)
        await asyncio.wait_for(started.wait(), 10)
        try:
            cancelled = await self.host.workflows.cancel(run["id"], project_id="p")
            self.assertEqual(cancelled["state"], "needs_review")
            self.assertIsNone(cancelled["stopped_at"])
            self.assertFalse(stopped.is_set())
        finally:
            release.set()
        await self.until(run["id"], "cancelled")
        self.assertTrue(stopped.is_set())
        self.assertEqual((await self.host.workflows.node_result(run["id"], "held", project_id="p"))["output"], {"value": 37})

    async def test_map_all_settled_retains_each_actual_failure(self):
        class MixedModel:
            async def complete(self, model, messages, **kwargs):
                value = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "user"))
                return Completion(text="" if value["value"] == 1 else str(value["value"]), usage={"prompt_tokens": 30, "completion_tokens": 2})
        self.host.model_api = MixedModel()
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker", outcome="all_settled", input={"value": "{{item}}"})
                 .node("end", "end", outputs={"items": "{{nodes.batch.outputs}}"}).connect("start", "batch").connect("batch", "end"))
        run = await self.run_graph(graph, {"items": [0, 1, 2]})
        result = await self.host.workflows.result(run["id"], project_id="p", timeout=15)
        self.assertEqual([item["state"] for item in result["items"]], ["completed", "failed", "completed"])
        self.assertEqual(result["items"][1]["error"]["code"], "EMPTY_OUTPUT")

    async def test_map_preserves_order_and_errors_for_unaccepted_members(self):
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker", outcome="all_settled", input={"value": "{{item.value}}"})
                 .node("end", "end", outputs={"items": "{{nodes.batch.outputs}}"}).connect("start", "batch").connect("batch", "end"))
        run = await self.run_graph(graph, {"items": [{"value": 11}, {}, {"value": 17}]})
        result = await self.host.workflows.result(run["id"], project_id="p", timeout=10)
        values = result["items"]
        self.assertEqual([item["state"] for item in values], ["completed", "failed", "completed"])
        self.assertEqual([json.loads(values[index]["text"])["value"] for index in (0, 2)], [11, 17])
        self.assertIsNone(values[1]["run_id"])
        self.assertEqual(values[1]["error"]["code"], "REFERENCE_UNAVAILABLE")
        self.assertEqual(self.host.model_api.calls, 2)
        members = await self.host.workflows.members(run["id"], "batch", project_id="p")
        self.assertEqual([item["ordinal"] for item in members["items"]], [0, 1, 2])
        self.assertEqual(members["items"][1]["error"]["code"], "REFERENCE_UNAVAILABLE")

    async def test_fail_fast_map_stops_owned_members_after_the_first_failure(self):
        held, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        independent, independent_release = asyncio.Event(), asyncio.Event()
        class FailAndHoldModel:
            async def complete(self, model, messages, **options):
                value = json.loads(next(message["content"] for message in messages if message["role"] == "user"))
                if value["index"] == 0:
                    await held.wait()
                    await independent.wait()
                    return Completion(text="", usage={"prompt_tokens": 30, "completion_tokens": 2})
                if value["index"] == 2:
                    independent.set()
                    await independent_release.wait()
                    return Completion(text="independent", usage={"prompt_tokens": 30, "completion_tokens": 2})
                held.set()
                try:
                    await release.wait()
                    return Completion(text="late", usage={"prompt_tokens": 30, "completion_tokens": 2})
                finally:
                    stopped.set()
        self.host.model_api = FailAndHoldModel()
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker", input={"index": "{{item}}"})
                 .node("end", "end", outputs={"items": "{{nodes.batch.outputs}}"}).connect("start", "batch").connect("batch", "end"))
        saved = await self.host.workflows.create("p", "Fail fast", graph.build())
        session = await self.host.sessions.create("p")
        run = await self.host.workflows.run(saved["id"], {"items": [0, 1]}, "first", project_id="p", session_id=session["id"])
        other = await self.host.workflows.run(saved["id"], {"items": [2]}, "other", project_id="p", session_id=session["id"])
        try:
            await asyncio.wait_for(independent.wait(), 10)
            await self.until(run["id"], "failed")
            self.assertTrue(stopped.is_set())
            self.assertEqual((await self.host.workflows.node_result(run["id"], "batch", project_id="p"))["state"], "failed")
            members = await self.host.store.call(lambda db: rows(db, "SELECT state,cancel_requested_at FROM runs WHERE workflow_run_id=? ORDER BY rowid", (run["id"],)))
            self.assertEqual(sorted(member["state"] for member in members), ["cancelled", "failed"])
            self.assertIsNotNone(next(member["cancel_requested_at"] for member in members if member["state"] == "cancelled"))
            untouched = await self.host.store.call(lambda db: one(db, "SELECT state,cancel_requested_at FROM runs WHERE workflow_run_id=?", (other["id"],)))
            self.assertEqual(untouched["state"], "running")
            self.assertIsNone(untouched["cancel_requested_at"])
            independent_release.set()
            result = await self.host.workflows.result(other["id"], project_id="p", timeout=10)
            self.assertEqual(result["items"][0]["text"], "independent")
        finally:
            release.set()
            independent_release.set()

    async def test_fail_fast_map_retains_unstopped_external_action(self):
        from oea import FunctionTool
        started, release = asyncio.Event(), asyncio.Event()
        submissions = []
        async def held_effect() -> dict:
            submissions.append(1)
            started.set()
            await release.wait()
            return {"accepted": True}
        self.host.register_tool(FunctionTool(held_effect, side_effects="external"))
        definition = AgentDefinition.model_validate((await self.host.agents.get("worker"))["definition"])
        definition.tools.functions = ("held_effect",)
        await self.host.agents.save(definition)
        class FailAndUseToolModel:
            async def complete(self, model, messages, **options):
                value = json.loads(next(message["content"] for message in messages if message["role"] == "user"))
                if value["index"] == 0:
                    await started.wait()
                    return Completion(text="", usage={"prompt_tokens": 30, "completion_tokens": 2})
                return Completion(tool_calls=({"id": "held", "type": "function", "function": {"name": "held_effect", "arguments": "{}"}},),
                                  finish_reason="tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 2})
        self.host.model_api = FailAndUseToolModel()
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker", input={"index": "{{index}}"})
                 .node("end", "end", outputs={"items": "{{nodes.batch.outputs}}"}).connect("start", "batch").connect("batch", "end"))
        run = await self.run_graph(graph, {"items": [0, 1]})
        try:
            await asyncio.wait_for(started.wait(), 10)
            pending = await self.until(run["id"], "needs_review")
            self.assertIsNone(pending["stopped_at"])
            child = await self.host.store.call(lambda db: one(db, "SELECT * FROM runs WHERE workflow_run_id=? AND state='needs_review'", (run["id"],)))
            self.assertIsNotNone(child)
            self.assertIsNotNone(child["cancel_requested_at"])
            self.assertIsNone(child["stopped_at"])
            self.assertEqual(submissions, [1])
            release.set()
            async with asyncio.timeout(10):
                while True:
                    changed = self.host.run_signal(child["id"])
                    current = await self.host.runs.get(child["id"])
                    if current["state"] == "cancelled":
                        break
                    await changed.wait()
            self.assertIsNotNone(current["stopped_at"])
            self.assertEqual(submissions, [1])
            async with asyncio.timeout(10):
                while True:
                    changed = self.host.workflows.signal(run["id"])
                    settled = await self.host.workflows.get_run(run["id"], project_id="p")
                    if settled["state"] == "waiting":
                        break
                    await changed.wait()
            self.assertIsNotNone(settled["stopped_at"])
            self.assertEqual(settled["wait"]["kind"], "confirmation")
        finally:
            release.set()

    async def test_resumed_graph_does_not_keep_a_previous_stop_confirmation(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def held() -> int:
            started.set()
            await release.wait()
            return 37
        self.host.workflows.register_function(held)
        graph = (WorkflowBuilder().node("start", "start").node("input", "wait").node("work", "function", name="held")
                 .node("end", "end", outputs={"value": "{{nodes.work.value}}"})
                 .connect("start", "input").connect("input", "work").connect("work", "end"))
        run = await self.run_graph(graph)
        waiting = await self.until(run["id"], "waiting", kind="input")
        self.assertIsNotNone(waiting["stopped_at"])
        try:
            await self.host.workflows.resume(run["id"], "reply", project_id="p", input="Continue")
            await asyncio.wait_for(started.wait(), 10)
            resumed = await self.host.workflows.get_run(run["id"], project_id="p")
            self.assertEqual(resumed["state"], "running")
            self.assertIsNone(resumed["stopped_at"])
        finally:
            release.set()
        self.assertEqual(await self.host.workflows.result(run["id"], project_id="p", timeout=10), {"value": 37})


    async def test_same_subgraph_can_run_in_two_independent_parent_workflows(self):
        child = (WorkflowBuilder().node("start", "start").node("end", "end", outputs={"value": "{{inputs.value}}"}).connect("start", "end"))
        saved_child = await self.host.workflows.create("p", "Child", child.build())
        parent = (WorkflowBuilder().node("start", "start").node("child", "subgraph", workflow_id=saved_child["id"], inputs={"value": "{{inputs.value}}"})
                  .node("end", "end", outputs={"value": "{{nodes.child.value}}"}).connect("start", "child").connect("child", "end"))
        saved = await self.host.workflows.create("p", "Parents", parent.build())
        first, second = await asyncio.gather(self.host.workflows.run(saved["id"], {"value": 11}, "first", project_id="p"),
            self.host.workflows.run(saved["id"], {"value": 17}, "second", project_id="p"))
        results = await asyncio.gather(*(self.host.workflows.result(run["id"], project_id="p", timeout=15) for run in (first, second)))
        self.assertEqual(results, [{"value": 11}, {"value": 17}])
        nested = await self.host.store.call(lambda db: rows(db, "SELECT parent_workflow_run_id,budget_id FROM workflow_runs WHERE workflow_id=?", (saved_child["id"],)))
        self.assertEqual({row["parent_workflow_run_id"] for row in nested}, {first["id"], second["id"]})
        self.assertEqual(len({row["budget_id"] for row in nested}), 2)

    async def test_map_has_no_hidden_128_member_capacity(self):
        from oea import Budget
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker", input={"index": "{{index}}"})
                 .node("end", "end", outputs={"items": "{{nodes.batch.outputs}}"}).connect("start", "batch").connect("batch", "end"))
        run = await self.run_graph(graph, {"items": list(range(129))}, budget=Budget(max_model_calls=256, max_output_tokens=1_048_576))
        # This checks completion beyond 128 members, not a wall-clock SLA.
        result = await self.host.workflows.result(run["id"], project_id="p", timeout=90)
        self.assertEqual(len(result["items"]), 129)
        self.assertEqual([json.loads(item["text"])["index"] for item in result["items"]], list(range(129)))
        self.assertEqual(self.host.model_api.calls, 129)


    async def test_restart_after_failed_repair_does_not_consume_the_same_request_again(self):
        calls = []
        def bad() -> int:
            calls.append(1)
            raise RuntimeFault("EXPECTED_FAILURE", "Still not repaired")
        self.host.workflows.register_function(bad)
        graph = (WorkflowBuilder().node("start", "start").node("bad", "function", name="bad")
                 .node("end", "end", outputs={"value": "{{nodes.bad.value}}"}).connect("start", "bad").connect("bad", "end"))
        run = await self.run_graph(graph, failure_policy="collect_independent")
        await self.until(run["id"], "failed")
        terminal_lost = asyncio.Event()
        original = self.host.workflows.finish
        async def lose_terminal(identity, state, **options):
            if identity == run["id"] and state == "failed":
                terminal_lost.set()
                raise RuntimeError("Simulated process loss before terminal receipt")
            return await original(identity, state, **options)
        with patch.object(self.host.workflows, "finish", lose_terminal):
            await self.host.workflows.resume(run["id"], "one-repair-request", project_id="p")
            await asyncio.wait_for(terminal_lost.wait(), 10)
            await asyncio.gather(*tuple(self.host.workflows.jobs.values()), return_exceptions=True)
        self.assertEqual(calls, [1, 1])
        self.assertEqual((await self.host.workflows.get_run(run["id"], project_id="p"))["state"], "running")
        await self.host.aclose()
        replacement = Host(self.root / "state", model_api=EchoModel())
        replacement.workflows.register_function(bad)
        self.host = await replacement.__aenter__()
        await self.until(run["id"], "failed")
        self.assertEqual(calls, [1, 1])
        await self.host.workflows.resume(run["id"], "one-repair-request", project_id="p")
        self.assertEqual(calls, [1, 1])


    async def test_declared_cooperative_function_cancel_observes_actual_cleanup(self):
        from oea import FunctionTool
        started, stopped = asyncio.Event(), asyncio.Event()
        async def cooperative() -> int:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.host.workflows.register_function(FunctionTool(cooperative, cancellation="cooperative"))
        graph = (WorkflowBuilder().node("start", "start").node("work", "function", name="cooperative")
                 .node("end", "end", outputs={}).connect("start", "work").connect("work", "end"))
        run = await self.run_graph(graph)
        await asyncio.wait_for(started.wait(), 10)
        cancelled = await self.host.workflows.cancel(run["id"], project_id="p")
        self.assertTrue(stopped.is_set())
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNotNone(cancelled["stopped_at"])


    async def test_repair_reuses_completed_map_snapshot_after_a_member_changes(self):
        from oea import RunSpec
        fixed_function, fixed_model = False, False
        class MixedModel:
            def __init__(self):
                self.calls = 0
            async def complete(self, model, messages, **options):
                self.calls += 1
                value = json.loads(next(message["content"] for message in messages if message["role"] == "user"))
                return Completion(text="" if value["value"] == 1 and not fixed_model else str(value["value"]), usage={"prompt_tokens": 30, "completion_tokens": 2})
        model = self.host.model_api = MixedModel()
        def verify() -> int:
            if not fixed_function:
                raise RuntimeFault("VERIFY_FAILURE", "Explicit repair is required")
            return 37
        self.host.workflows.register_function(verify)
        graph = (WorkflowBuilder().node("start", "start").node("batch", "map", agent_id="worker", outcome="all_settled", input={"value": "{{item}}"})
                 .node("verify", "function", name="verify").node("end", "end", outputs={"items": "{{nodes.batch.outputs}}", "verified": "{{nodes.verify.value}}"})
                 .connect("start", "batch").connect("start", "verify").connect("batch", "end").connect("verify", "end"))
        run = await self.run_graph(graph, {"items": [0, 1, 2]}, failure_policy="collect_independent")
        await self.until(run["id"], "failed")
        before = await self.host.workflows.node_result(run["id"], "batch", project_id="p")
        failed_child = before["output"]["outputs"][1]["run_id"]
        fixed_model = True
        await self.host.runs.resume(failed_child, "repair-original-child")
        await self.host.runs.result(failed_child, timeout=10)
        self.assertEqual(model.calls, 4)
        snapshot = await self.host.workflows.members(run["id"], "batch", project_id="p", version=before["version"], limit=1, cursor=1)
        self.assertEqual(snapshot["items"][0]["state"], "failed")
        self.assertEqual(snapshot["items"][0]["run_id"], failed_child)
        fixed_function = True
        await self.host.workflows.resume(run["id"], "repair-verification", project_id="p", node_id="verify")
        after = await self.host.workflows.result(run["id"], project_id="p", timeout=15)
        self.assertEqual(after["verified"], 37)
        self.assertEqual(after["items"][1]["state"], "failed")
        self.assertEqual(model.calls, 4)
        self.assertEqual((await self.host.workflows.node_result(run["id"], "batch", project_id="p"))["version"], before["version"])
        self.assertEqual(len(await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs WHERE workflow_run_id=?", (run["id"],)))), 3)


if __name__ == "__main__":
    unittest.main()
