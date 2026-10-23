"""Repairs create new execution ownership and retain successful evidence by version."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from oea import Runtime, RuntimeConfig, RuntimeFault, WorkflowBuilder
from test_instances import Worker


class WorkflowRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-workflow-repair-")
        self.worker = Worker()
        self.rt = await Runtime.open(Path(self.temp.name), RuntimeConfig(max_concurrency=4, reserved_external_slots=0),
                                     adapters={"worker": self.worker}).__aenter__()

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def terminal(self, run_id):
        async with asyncio.timeout(5):
            while (run := await self.rt.workflows.get_run(run_id)).ended_at is None:
                await asyncio.sleep(0.02)
            return run

    async def make_map(self, mode="all_succeeded"):
        graph = (WorkflowBuilder().node("start", "start", failure_policy="collect_independent")
            .node("map", "map", agent_ids=["worker"], result_mode=mode, payload={"ordinal": "{{index}}", "fail": "{{item}}"})
            .node("good", "agent", agent_id="worker", payload={"stable": True})
            .node("goodend", "end", outputs={"stable": "{{nodes.good.stable}}"})
            .node("end", "end", outputs={"members": "{{nodes.map.results}}"} if mode == "all_settled" else {"members": "{{nodes.map.outputs}}"})
            .connect("start", "map").connect("map", "end").connect("start", "good").connect("good", "goodend").build())
        return await self.rt.workflows.create("default", "Repairable", "flow", graph=graph)

    async def test_repair_only_runs_failed_members_and_preserves_source_ownership(self):
        saved = await self.make_map()
        source = await self.rt.workflows.run(saved.id, {"items": [False, True, False]}, "source")
        original = await self.terminal(source.id)
        self.assertEqual(original.state, "FAILED")
        original_json = original.model_dump_json()
        successful = [member for node in original.nodes if node.node_id == "map" for member in node.instances if member["state"] == "SUCCEEDED"]
        before = len(self.worker.seen)
        preview = await self.rt.workflows.preview_repair(source.id, inputs={"items": [False, False, False]})
        self.assertEqual(set(preview["reuse_candidates"]), {"good", "goodend"})
        self.assertEqual(preview["map_members"][0]["reusable_ordinals"], [0, 2])
        self.assertEqual(preview["map_members"][0]["rerun_ordinals"], [1])
        self.assertEqual(len(self.worker.seen), before)
        repair = await self.rt.workflows.repair(source.id, "repair", inputs={"items": [False, False, False]})
        completed = await self.terminal(repair.id)
        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(len(self.worker.seen) - before, 1)
        self.assertEqual(self.worker.seen[-1]["ordinal"], 1)
        self.assertEqual((await self.rt.workflows.get_run(source.id)).model_dump_json(), original_json)
        evidence = await self.rt.workflows.get_repair(repair.id)
        self.assertEqual(evidence["invalidated_nodes"], preview["invalidated_nodes"])
        self.assertEqual({row["node_id"] for row in evidence["reused_nodes"]}, {"good", "goodend"})
        self.assertEqual([row["ordinal"] for row in evidence["members"] if row["source_run_id"]], [0, 2])
        current = next(node for node in completed.nodes if node.node_id == "map")
        self.assertEqual([member["ordinal"] for member in current.instances], [0, 1, 2])
        self.assertEqual([item["ordinal"] for item in current.output["outputs"]], [0, 1, 2])
        for old in successful:
            self.assertEqual((await self.rt.threads.ownership(old["thread_id"]))["workflow_run_id"], source.id)
        self.assertTrue((await self.rt.workflows.repair(source.id, "repair", inputs={"items": [False, False, False]})).reused)
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.workflows.repair(source.id, "repair", inputs={"items": []})
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_all_settled_success_can_repair_bad_members_and_repair_can_be_repaired(self):
        saved = await self.make_map("all_settled")
        source = await self.rt.workflows.run(saved.id, {"items": [False, True, True]}, "source")
        self.assertEqual((await self.terminal(source.id)).state, "SUCCEEDED")
        before = len(self.worker.seen)
        first = await self.rt.workflows.repair(source.id, "first", inputs={"items": [False, False, True]})
        self.assertEqual((await self.terminal(first.id)).state, "SUCCEEDED")
        self.assertEqual(len(self.worker.seen) - before, 2)
        second = await self.rt.workflows.repair(first.id, "second", inputs={"items": [False, False, False]})
        final = await self.terminal(second.id)
        self.assertEqual(len(self.worker.seen) - before, 3)
        members = next(node.output["results"] for node in final.nodes if node.node_id == "map")
        self.assertEqual([member["state"] for member in members], ["SUCCEEDED"] * 3)
        self.assertEqual(members[0]["source_run_id"], source.id)
        self.assertEqual(members[1]["source_run_id"], first.id)

    async def test_preview_map_contract_change_reports_full_redo_without_dispatch(self):
        saved = await self.make_map()
        source = await self.rt.workflows.run(saved.id, {"items": [False, False, False]}, "source")
        await self.terminal(source.id)
        graph = saved.graph.model_dump(mode="json")
        next(node for node in graph["nodes"] if node["id"] == "map")["config"]["items"] = [False, False]
        changed = await self.rt.workflows.update(saved.id, saved.revision, graph=graph)
        before = len(self.worker.seen)
        preview = await self.rt.workflows.preview_repair(source.id, expected_revision=changed.revision)
        member_plan = preview["map_members"][0]
        self.assertEqual(member_plan["reason"], "map_contract_changed")
        self.assertEqual(member_plan["reusable_ordinals"], [])
        self.assertEqual(member_plan["rerun_ordinals"], [0, 1])
        self.assertEqual(len(self.worker.seen), before)
        repaired = await self.rt.workflows.repair(source.id, "changed-items", expected_revision=changed.revision)
        self.assertEqual((await self.terminal(repaired.id)).state, "SUCCEEDED")
        self.assertEqual(len(self.worker.seen) - before, 2)

    async def test_preview_does_not_guess_invalidated_upstream_map_inputs(self):
        graph = (WorkflowBuilder().node("start", "start")
            .node("source", "agent", agent_id="worker", payload={"items": [False]})
            .node("map", "map", agent_ids=["worker"], items={"$ref": "nodes.source.items", "default": []}, payload={"fail": "{{item}}"})
            .node("end", "end", outputs={"members": "{{nodes.map.outputs}}"})
            .connect("start", "source").connect("source", "map").connect("map", "end").build())
        saved = await self.rt.workflows.create("default", "Dependent map", "dependent", graph=graph)
        source = await self.rt.workflows.run(saved.id, {}, "source")
        await self.terminal(source.id)
        preview = await self.rt.workflows.preview_repair(source.id, rerun_nodes=["source"])
        self.assertEqual(preview["map_members"], [{"node_id": "map", "status": "UNKNOWN", "reason": "upstream_output_unavailable"}])

    async def test_result_policy_change_revalidates_successful_members(self):
        saved = await self.make_map()
        source = await self.rt.workflows.run(saved.id, {"items": [False, False, False]}, "source")
        original = await self.terminal(source.id)
        before = len(self.worker.seen)
        graph = saved.graph.model_dump(mode="json")
        map_node = next(node for node in graph["nodes"] if node["id"] == "map")
        map_node["config"]["result_policy"] = {"kind": "structured", "output_schema": {
            "type": "object", "required": ["ordinal"]}}
        changed = await self.rt.workflows.update(saved.id, saved.revision, graph=graph)
        repair = await self.rt.workflows.repair(source.id, "compatible-policy", expected_revision=changed.revision)
        first = await self.terminal(repair.id)
        self.assertEqual(first.state, "SUCCEEDED")
        self.assertEqual(len(next(node for node in first.nodes if node.node_id == "map").instances), 3)
        self.assertEqual(len(self.worker.seen), before)
        evidence = await self.rt.workflows.get_repair(repair.id)
        self.assertEqual(len([member for member in evidence["members"] if member["source_run_id"]]), 3)
        map_node["config"]["result_policy"]["output_schema"]["properties"] = {"ordinal": {"minimum": 1}}
        stricter = await self.rt.workflows.update(saved.id, changed.revision, graph=graph)
        second = await self.rt.workflows.repair(repair.id, "stricter-policy", expected_revision=stricter.revision)
        self.assertEqual((await self.terminal(second.id)).state, "FAILED")
        self.assertEqual(len(self.worker.seen), before + 1)
        evidence = await self.rt.workflows.get_repair(second.id)
        self.assertEqual([member["ordinal"] for member in evidence["members"] if member["source_run_id"]], [1, 2])
        self.assertEqual((await self.rt.workflows.get_run(source.id)).model_dump_json(), original.model_dump_json())

    async def test_explicit_definition_revision_invalidates_changed_node_and_descendants(self):
        graph = (WorkflowBuilder().node("start", "start", failure_policy="collect_independent")
            .node("bad", "template", template="{{inputs.missing}}")
            .node("end", "end", outputs={"text": "{{nodes.bad.text}}"})
            .connect("start", "bad").connect("bad", "end").build())
        saved = await self.rt.workflows.create("default", "Fix definition", "definition", graph=graph)
        source = await self.rt.workflows.run(saved.id, {}, "source")
        await self.terminal(source.id)
        updated = graph.model_dump(mode="json")
        updated["nodes"][1]["config"]["template"] = "Corrected"
        changed = await self.rt.workflows.update(saved.id, saved.revision, graph=updated)
        old_graph_repair = await self.rt.workflows.repair(source.id, "frozen")
        self.assertEqual((await self.terminal(old_graph_repair.id)).state, "FAILED")
        repaired = await self.rt.workflows.repair(source.id, "updated", expected_revision=changed.revision)
        final = await self.terminal(repaired.id)
        self.assertEqual(final.state, "SUCCEEDED")
        self.assertEqual(final.revision, changed.revision)
        self.assertEqual(final.result, {"outputs": {"text": "Corrected"}})
