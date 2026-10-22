"""Independent branches and explicit map aggregation on the real scheduler."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from oea import AgentConfig, Runtime, RuntimeConfig, RuntimeFault, WorkflowBuilder
from ma_runtime.models import WorkflowGraph
from test_instances import Worker


class WorkflowPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-policy-")
        self.worker = Worker()
        self.rt = await Runtime.open(Path(self.temp.name), RuntimeConfig(max_concurrency=4, reserved_external_slots=0),
                                     adapters={"worker": self.worker}).__aenter__()

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def wait(self, run_id, states):
        async with asyncio.timeout(5):
            while (run := await self.rt.workflows.get_run(run_id)).state not in states:
                await asyncio.sleep(0.025)
            return run

    async def test_collect_independent_preserves_success_and_blocks_failed_dependencies(self):
        for policy in ("fail_fast", "collect_independent"):
            graph = (WorkflowBuilder().node("start", "start", failure_policy=policy)
                .node("broken", "template", template="{{inputs.absent}}")
                .node("good", "agent", agent_id="worker", payload={"evidence": "kept"})
                .node("blocked", "template", template="must not execute")
                .node("badend", "end", outputs={})
                .node("goodend", "end", outputs={"evidence": "{{nodes.good.evidence}}"})
                .connect("start", "broken").connect("start", "good")
                .connect("broken", "blocked").connect("good", "blocked")
                .connect("blocked", "badend").connect("good", "goodend").build())
            saved = await self.rt.workflows.create("default", policy, policy, graph=graph)
            handle = await self.rt.workflows.run(saved.id, {}, policy)
            run = await self.wait(handle.id, {"FAILED"})
            nodes = {node.node_id: node for node in run.nodes}
            self.assertEqual(nodes["broken"].state, "FAILED")
            self.assertEqual(nodes["blocked"].state, "SKIPPED")
            self.assertEqual(nodes["badend"].state, "SKIPPED")
            if policy == "collect_independent":
                self.assertEqual(nodes["good"].state, "SUCCEEDED")
                self.assertEqual(nodes["blocked"].error["code"], "DEPENDENCY_FAILED")
                self.assertEqual(run.result, {"outputs": {"evidence": "kept"}})
            else:
                self.assertNotEqual(nodes["good"].state, "SUCCEEDED")
            self.assertEqual(run.error["code"], "VARIABLE_NOT_FOUND")

    async def test_all_settled_has_complete_ordered_member_results_and_errors(self):
        graph = (WorkflowBuilder().node("start", "start", failure_policy="collect_independent")
            .node("map", "map", agent_ids=["worker"], result_mode="all_settled",
                  payload={"index": "{{index}}", "fail": "{{item.fail}}", "delay": "{{item.delay}}"})
            .node("end", "end", outputs={"members": "{{nodes.map.results}}"})
            .connect("start", "map").connect("map", "end").build())
        saved = await self.rt.workflows.create("default", "Settled", "settled", graph=graph)
        handle = await self.rt.workflows.run(saved.id, {"items": [
            {"fail": False, "delay": 0.1}, {"fail": True, "delay": 0}, {"fail": False, "delay": 0.02}]}, "run")
        result = await self.rt.workflows.result(handle.id, timeout=5)
        members = result["outputs"]["members"]
        self.assertEqual([member["ordinal"] for member in members], [0, 1, 2])
        self.assertEqual([member["state"] for member in members], ["SUCCEEDED", "FAILED", "SUCCEEDED"])
        self.assertEqual(members[0]["result"]["output"]["index"], 0)
        self.assertIsNone(members[1]["result"])
        self.assertIsNotNone(members[1]["error"])
        self.assertEqual(members[2]["result"]["output"]["index"], 2)
        run = await self.rt.workflows.get_run(handle.id)
        output = next(n.output for n in run.nodes if n.node_id == "map")
        self.assertNotIn("outputs", output)
        self.assertEqual(output["counts"], {"SUCCEEDED": 2, "FAILED": 1})
        empty = await self.rt.workflows.run(saved.id, {"items": []}, "empty")
        self.assertEqual((await self.rt.workflows.result(empty.id, timeout=3))["outputs"]["members"], [])

    async def test_independent_all_success_map_does_not_cancel_healthy_members(self):
        graph = (WorkflowBuilder().node("start", "start", failure_policy="collect_independent")
            .node("map", "map", agent_ids=["worker"], payload={"index": "{{index}}", "fail": "{{item}}"})
            .node("end", "end", outputs={})
            .connect("start", "map").connect("map", "end").build())
        saved = await self.rt.workflows.create("default", "Partial", "partial", graph=graph)
        handle = await self.rt.workflows.run(saved.id, {"items": [True, False, False]}, "run")
        run = await self.wait(handle.id, {"FAILED"})
        node = next(n for n in run.nodes if n.node_id == "map")
        self.assertEqual(node.output["counts"], {"FAILED": 1, "SUCCEEDED": 2})
        self.assertEqual([m["ordinal"] for m in node.output["results"]], [0, 1, 2])

    async def test_unrelated_failure_does_not_prevent_resuming_an_independent_wait(self):
        await self.rt.agents.configure(AgentConfig(id="waiter", name="Waiter", kind="mock"))
        graph = (WorkflowBuilder().node("start", "start", failure_policy="collect_independent")
            .node("broken", "template", template="{{inputs.absent}}")
            .node("waiter", "agent", agent_id="waiter", payload={"input": "wait", "_mock": {"delay": 0, "wait_for_input": True}})
            .node("badend", "end", outputs={}).node("end", "end", outputs={"text": "{{nodes.waiter.text}}"})
            .connect("start", "broken").connect("broken", "badend")
            .connect("start", "waiter").connect("waiter", "end").build())
        saved = await self.rt.workflows.create("default", "Wait", "wait", graph=graph)
        handle = await self.rt.workflows.run(saved.id, {}, "run")
        run = await self.wait(handle.id, {"WAITING"})
        task_id = next(n.task_id for n in run.nodes if n.node_id == "waiter")
        wait = (await self.rt.tasks.get(task_id)).wait
        await self.rt.workflows.resume_node(handle.id, "waiter", {"answer": "ready", "wait_token": wait["wait_token"]}, "resume")
        final = await self.wait(handle.id, {"FAILED"})
        self.assertEqual(next(n.state for n in final.nodes if n.node_id == "waiter"), "SUCCEEDED")
        self.assertIn("text", final.result["outputs"])

    def test_default_graph_shape_remains_compatible(self):
        graph = (WorkflowBuilder().node("start", "start").node("map", "map", agent_ids=["worker"])
                 .node("end", "end").connect("start", "map").connect("map", "end").build())
        normalized = WorkflowGraph.model_validate_json(graph.model_dump_json())
        self.assertNotIn("failure_policy", normalized.nodes[0].config)
        self.assertNotIn("result_mode", normalized.nodes[1].config)
