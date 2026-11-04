"""Published Agent architectures share acceptance, recovery and permission boundaries."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oea import AgentArchitecture, AgentDefinition, ArchitectureBinding, ArchitectureBuilder, ArchitectureClient, Completion, Execution, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.orchestration.architecture_templates import templates, react
from oea.orchestration.architecture_validation import validate_graph
from oea.runtime.host import Host
from oea.runtime.context import RunContext
from oea.storage.persistence import one, rows
from oea.tools import ToolKit, tool


class TextModel:
    def __init__(self):
        self.calls = 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    async def complete(self, model, messages, **options):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return Completion(text="Complete PLAN_DONE ACCEPT_RESULT", usage={"prompt_tokens": 20, "completion_tokens": 5})


class ArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-architecture-")
        self.addCleanup(self.temp.cleanup)
        self.model = TextModel()
        self.host = await Host(Path(self.temp.name) / "state", model_api=self.model).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.projects.create("Example", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        await self.host.agents.save(AgentDefinition(id="agent", project_id="p", name="Agent", model=Model(base_url="http://model.invalid", model="test")))
        await self.host.sessions.create("p", "Session", session_id="session")
        self.instance = await self.host.instances.create("session", "agent", name="one")

    async def asyncTearDown(self):
        self.model.release.set()
        await self.host.aclose()
        self.temp.cleanup()

    async def candidate(self, name="react", identity="architecture"):
        graph = templates()[name]
        await self.host.architectures.create(AgentArchitecture(id=identity, project_id="p", name=name), graph.model_dump(mode="json"))
        version = await self.host.architectures.publish(identity, "p", 1)
        return ArchitectureBinding(architecture_id=identity, version=version["version"])

    async def bind(self, binding):
        saved = await self.host.agents.get("agent")
        return await self.host.architectures.bind("agent", "p", binding, saved["revision"])

    async def submit(self, key="work"):
        return await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Complete a bounded task", idempotency_key=key))

    async def settle(self, handle):
        await self.host.jobs[handle.id]
        return await self.host.runs.get(handle.id)

    async def test_templates_compile_and_execute_without_default_extra_calls(self):
        for index, name in enumerate(templates()):
            with self.subTest(template=name):
                binding = await self.candidate(name, "architecture-" + str(index))
                await self.bind(binding)
                before = self.model.calls
                handle = await self.submit(name)
                result = await self.settle(handle)
                self.assertEqual(result["state"], "completed", result)
                self.assertEqual(self.model.calls - before, 1 if name == "react" else 2)
                snapshot = await self.host.store.call(lambda db: one(db, "SELECT config_json FROM runs WHERE id=?", (handle.id,)))
                self.assertEqual(json.loads(snapshot["config_json"])["execution"]["architecture"], binding.model_dump())
                steps = await self.host.store.call(lambda db: rows(db, "SELECT node_id FROM architecture_steps WHERE run_id=?", (handle.id,)))
                self.assertIn("deliver", {row["node_id"] for row in steps})

    async def test_drafts_publish_immutably_and_shared_candidates_do_not_bind(self):
        binding = await self.candidate()
        saved = await self.host.architectures.get(binding.architecture_id, "p")
        original = saved["draft"]
        await self.host.architectures.save(binding.architecture_id, "p", 1, {"incomplete": True, "advanced": {"keep": 1}})
        report = await self.host.architectures.validate(binding.architecture_id, "p")
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"][0]["path"])
        with self.assertRaises(RuntimeFault) as failure:
            await self.host.architectures.publish(binding.architecture_id, "p", 2)
        self.assertEqual(failure.exception.code, "ARCHITECTURE_INVALID")
        version = await self.host.architectures.get(binding.architecture_id, "p", version=1)
        self.assertEqual(version["definition"], original)
        self.assertIsNone((await self.host.agents.get("agent"))["definition"]["execution"]["architecture"])
        with self.assertRaises(RuntimeFault):
            await self.host.architectures.get(binding.architecture_id, "q")

    async def test_published_dependencies_are_checked_before_execution(self):
        await self.bind(await self.candidate())
        with patch("oea.orchestration.registry.package_version", return_value="0.0"):
            run = await self.submit("dependency-change")
            result = await self.settle(run)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "ARCHITECTURE_DEPENDENCY_MISMATCH")
        self.assertEqual(self.model.calls, 0)

    async def test_switch_blocked_for_pending_work_in_all_entry_points(self):
        binding = await self.candidate()
        self.model.release.clear()
        handle = await self.submit()
        await asyncio.wait_for(self.model.entered.wait(), 5)
        await self.host.architectures.save(binding.architecture_id, "p", 1, react().model_dump(mode="json"))
        await self.host.architectures.publish(binding.architecture_id, "p", 2)
        for operation in ("bind", "save"):
            with self.subTest(operation=operation), self.assertRaises(RuntimeFault) as failure:
                if operation == "bind":
                    await self.host.registry.call("architectures.bind", {"agent_id": "agent", "project_id": "p", "binding": binding.model_dump(), "expected_revision": 1})
                else:
                    saved = await self.host.agents.get("agent")
                    saved["definition"]["execution"]["architecture"] = binding.model_dump()
                    await self.host.registry.call("agents.save", {"definition": saved["definition"], "expected_revision": saved["revision"]})
            self.assertEqual(failure.exception.code, "AGENT_IN_USE")
        self.model.release.set()
        self.assertEqual((await self.settle(handle))["state"], "completed")
        await self.bind(binding)

    async def test_atomic_acceptance_and_binding_have_a_serial_order(self):
        binding = await self.candidate()
        self.model.release.clear()
        accepted, switched = await asyncio.gather(self.submit(), self.bind(binding), return_exceptions=True)
        self.assertFalse(isinstance(accepted, Exception))
        snapshot = await self.host.store.call(lambda db: one(db, "SELECT config_json FROM runs WHERE id=?", (accepted.id,)))
        bound = json.loads(snapshot["config_json"])["execution"]["architecture"]
        if isinstance(switched, RuntimeFault):
            self.assertEqual(switched.code, "AGENT_IN_USE")
            self.assertIsNone(bound)
        else:
            self.assertEqual(bound, binding.model_dump())
        self.model.release.set()
        await self.settle(accepted)

    async def test_unknown_actions_block_even_when_run_is_terminal(self):
        binding = await self.candidate()
        handle = await self.submit()
        await self.settle(handle)
        await self.host.store.call(lambda db: db.execute("UPDATE actions SET state='needs_review' WHERE run_id=?", (handle.id,)), transaction=True)
        with self.assertRaises(RuntimeFault) as failure:
            await self.bind(binding)
        self.assertEqual(failure.exception.code, "AGENT_IN_USE")

    async def test_export_roundtrip_and_bounded_preview(self):
        binding = await self.candidate()
        exported = await self.host.architectures.export(binding.architecture_id, "p", version=1)
        namespace = {}
        exec(exported["source"], namespace)
        self.assertEqual(namespace["graph"], react())
        view = await self.host.architectures.preview(binding.architecture_id, "p", version=1)
        self.assertLessEqual(len(view["stages"]), 8)
        self.assertNotIn("nodes", view)
        self.assertNotIn("draft", view)
        with patch.object(self.host.architectures, "_preview", side_effect=AssertionError("Preview reads must not traverse the declaration")):
            self.assertEqual((await self.host.architectures.preview(binding.architecture_id, "p"))["stages"], view["stages"])
            self.assertEqual((await self.host.architectures.preview(binding.architecture_id, "p", version=1))["stages"], view["stages"])
        draft = (await self.host.architectures.get(binding.architecture_id, "p"))["draft"]
        draft["metadata"] = {"lines": "first\nsecond", "path": r"C:\new\file.py", "quote": "''' and \"quoted\" 中文"}
        await self.host.architectures.save(binding.architecture_id, "p", 1, draft)
        exported = await self.host.architectures.export(binding.architecture_id, "p")
        namespace = {}
        exec(exported["source"], namespace)
        self.assertEqual(namespace["graph"].metadata, draft["metadata"])

    async def test_restart_after_model_receipt_keeps_original_action_identity(self):
        binding = await self.candidate()
        await self.bind(binding)
        original = RunContext.call_model
        crashed = False
        async def interrupted(context, state):
            nonlocal crashed
            result = await original(context, state)
            if not crashed:
                crashed = True
                raise RuntimeError("Receipt committed before graph position")
            return result
        with patch.object(RunContext, "call_model", interrupted):
            handle = await self.submit()
            self.assertEqual((await self.settle(handle))["state"], "failed")
        await self.host.aclose()
        self.host = await Host(Path(self.temp.name) / "state", model_api=self.model).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.runs.resume(handle.id, "resume")
        self.assertEqual((await self.settle(handle))["state"], "completed")
        self.assertEqual(self.model.calls, 1)
        actions = await self.host.store.call(lambda db: rows(db, "SELECT operation_key FROM actions WHERE run_id=?", (handle.id,)))
        self.assertEqual(len(actions), 1)

    async def test_restart_after_tool_receipt_does_not_repeat_effect(self):
        effects = []
        @tool(side_effects="local")
        def record_value(value: int) -> dict:
            effects.append(value)
            return {"value": value}
        class ToolModel:
            async def complete(self, model, messages, **options):
                observed = any(message["role"] == "tool" for message in messages)
                calls = () if observed else ({"id": "record", "type": "function", "function": {"name": "record_value", "arguments": '{"value":7}'}},)
                return Completion(text="Recorded" if observed else "", tool_calls=calls,
                    finish_reason="stop" if observed else "tool_calls", usage={"prompt_tokens": 20, "completion_tokens": 5})
        self.host.model_api = ToolModel()
        self.host.register_tool(record_value)
        saved = await self.host.agents.get("agent")
        definition = AgentDefinition.model_validate(saved["definition"]).model_copy(update={"tools": ToolKit.local(files=False).with_tools(record_value).bindings.model_copy(update={"loading": "eager"})})
        await self.host.agents.save(definition, expected_revision=saved["revision"])
        await self.bind(await self.candidate())
        original = RunContext.execute_tools
        async def interrupted(context, state):
            await original(context, state)
            raise RuntimeError("Tool receipt committed before checkpoint")
        with patch.object(RunContext, "execute_tools", interrupted):
            handle = await self.submit()
            self.assertEqual((await self.settle(handle))["state"], "failed")
        self.assertEqual(effects, [7])
        api = self.host.model_api
        await self.host.aclose()
        self.host = Host(Path(self.temp.name) / "state", model_api=api)
        self.host.register_tool(record_value)
        await self.host.__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.runs.resume(handle.id, "resume")
        result = await self.settle(handle)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(effects, [7])

    async def test_wait_restart_and_current_revocation(self):
        graph = ArchitectureBuilder(react())
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "wait")
        await self.host.architectures.create(AgentArchitecture(id="waiting", project_id="p", name="Waiting"), graph.build().model_dump(mode="json"))
        await self.host.architectures.publish("waiting", "p", 1)
        await self.bind(ArchitectureBinding(architecture_id="waiting", version=1))
        handle = await self.submit()
        self.assertEqual((await self.settle(handle))["state"], "waiting")
        await self.host.aclose()
        self.host = await Host(Path(self.temp.name) / "state", model_api=self.model).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.runs.resume(handle.id, "reply", input="Continue")
        self.assertEqual((await self.settle(handle))["state"], "completed")
        self.assertEqual(self.model.calls, 1)

    async def test_project_delete_clears_owned_architectures(self):
        await self.candidate()
        project = await self.host.projects.get("p")
        await self.host.projects.delete("p", confirm_name=project["name"], idempotency_key="delete")
        counts = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS value FROM agent_architectures WHERE project_id='p'"))
        self.assertEqual(counts["value"], 0)

    async def test_branch_edit_changes_observed_path(self):
        graph = ArchitectureBuilder(react()).state("choose", type="boolean", default=False)
        graph.node("choice", "condition", field="choose")
        graph.node("left", "set", values={"choose": True}).node("right", "set", values={"choose": False})
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "choice").connect("choice", "left", branch="true").connect("choice", "right", branch="false")
        graph.connect("left", "model").connect("right", "model")
        for index, choose in enumerate((False, True)):
            graph.value["state"]["choose"]["default"] = choose
            identity = "branch-" + str(index)
            await self.host.architectures.create(AgentArchitecture(id=identity, project_id="p", name="Branch"), graph.build().model_dump(mode="json"))
            await self.host.architectures.publish(identity, "p", 1)
            await self.bind(ArchitectureBinding(architecture_id=identity, version=1))
            handle = await self.submit(identity)
            result = await self.settle(handle)
            self.assertEqual(result["state"], "completed", result)
            steps = await self.host.store.call(lambda db: rows(db, "SELECT node_id FROM architecture_steps WHERE run_id=?", (handle.id,)))
            visited = {row["node_id"] for row in steps}
            self.assertIn("left" if choose else "right", visited)
            self.assertNotIn("right" if choose else "left", visited)

    async def run_graph(self, graph, identity):
        await self.host.architectures.create(AgentArchitecture(id=identity, project_id="p", name=identity), graph.build().model_dump(mode="json"))
        await self.host.architectures.publish(identity, "p", 1)
        await self.bind(ArchitectureBinding(architecture_id=identity, version=1))
        handle = await self.submit(identity)
        result = await self.settle(handle)
        self.assertEqual(result["state"], "completed", result)
        return handle

    async def node_output(self, run_id, name):
        detail = await self.host.architectures.inspect(run_id, "p", node_id=name)
        row = detail["items"][-1]
        return (await self.host.architectures.node_result(run_id, "p", row["scope"], name, row["activation"]))["output"]

    async def test_parallel_unequal_paths_merge_before_context_writer(self):
        graph = ArchitectureBuilder(react()).state("total", type="integer", reducer="sum", default=10).state("observed", type="integer")
        graph.state("right_seen", type="integer")
        graph.node("fork", "set").node("left", "set", values={"total": 2}).node("right", "set", values={"total": 3})
        graph.node("longer", "set", values={"total": 4, "right_seen": "$total"}).node("merge", "set", values={"observed": "$total"})
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "fork").connect("fork", "left").connect("fork", "right")
        graph.connect("left", "merge").connect("right", "longer").connect("longer", "merge").connect("merge", "model")
        handle = await self.run_graph(graph, "parallel")
        self.assertEqual(await self.node_output(handle.id, "merge"), {"observed": 19})
        self.assertEqual((await self.node_output(handle.id, "longer"))["right_seen"], 13)
        self.assertEqual(self.model.calls, 1)

    async def test_pure_function_error_is_recorded_at_the_actual_node(self):
        def fail() -> dict:
            raise ValueError("Invalid computation")
        self.host.workflows.register_function(fail)
        graph = ArchitectureBuilder(react()).node("compute", "function", name="fail")
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "compute").connect("compute", "model")
        await self.host.architectures.create(AgentArchitecture(id="failed-node", project_id="p", name="Failed node"), graph.build().model_dump(mode="json"))
        await self.host.architectures.publish("failed-node", "p", 1)
        await self.bind(ArchitectureBinding(architecture_id="failed-node", version=1))
        run = await self.submit("failed-node")
        self.assertEqual((await self.settle(run))["state"], "failed")
        detail = await self.host.architectures.inspect(run.id, "p", node_id="compute")
        self.assertEqual(detail["items"][0]["state"], "failed")
        self.assertEqual(detail["items"][0]["error"]["details"]["cause"], "ValueError")
        self.assertEqual(self.model.calls, 0)

    async def test_computed_state_maps_into_model_input_without_extra_model_calls(self):
        def explain(value: int) -> str:
            return "Derived result: " + str(value * 6)
        class Captured(TextModel):
            async def complete(inner, model, messages, **options):
                inner.messages = messages
                return await super().complete(model, messages, **options)
        self.host.model_api = captured = Captured()
        self.host.workflows.register_function(explain)
        graph = ArchitectureBuilder(react()).state("count", type="integer", default=7).state("prompt", type="string")
        graph.node("compute", "function", name="explain", arguments={"value": "$count"}, outputs={"value": "prompt"})
        node = next(node for node in graph.value["nodes"] if node["id"] == "model")
        node["inputs"] = {"request": "$prompt"}
        node["config"] = {"instruction": "Use the calculated evidence.", "instruction_field": "request"}
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "compute").connect("compute", "model")
        await self.run_graph(graph, "mapped-model")
        self.assertIn("Use the calculated evidence.\n\nDerived result: 42", [message.get("content") for message in captured.messages])
        self.assertEqual(captured.calls, 1)

    async def test_dynamic_send_merges_members_and_empty_input(self):
        def twice(value: int) -> dict:
            return {"value": [value * 2]}
        self.host.workflows.register_function(twice)
        for index, values in enumerate(([2, 3, 5], [])):
            graph = ArchitectureBuilder(react()).state("items", type="array", default=values)
            graph.state("member", type="integer").state("answers", type="array", reducer="append", default=[]).state("observed", type="array")
            graph.node("dispatch", "dispatch", items="items", target="member", item_field="member")
            graph.node("member", "function", name="twice", arguments={"value": "$member"}, outputs={"value": "answers"})
            graph.node("merge", "set", values={"observed": "$answers"})
            graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
            graph.connect("prepare", "dispatch").connect("dispatch", "member").connect("member", "merge").connect("merge", "model")
            handle = await self.run_graph(graph, "send-" + str(index))
            self.assertEqual((await self.node_output(handle.id, "merge"))["observed"], [value * 2 for value in values])
            visited = await self.host.architectures.inspect(handle.id, "p", node_id="member")
            self.assertEqual(len(visited["items"]), len(values))
            self.assertEqual(len({row["activation"] for row in visited["items"]}), len(values))

    async def test_subgraph_maps_outputs_without_redelivering_or_double_reduction(self):
        child = ArchitectureBuilder().state("amount", type="integer", default=0)
        child.node("increment", "set", values={"amount": 7}).node("end", "deliver").connect("increment", "end")
        graph = ArchitectureBuilder(react()).state("total", type="integer", reducer="sum", default=0).state("observed", type="integer")
        graph.node("nested", "subgraph", graph=child.build().model_dump(mode="json"), outputs={"amount": "total"})
        graph.node("merged", "set", values={"observed": "$total"})
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "nested").connect("nested", "merged").connect("merged", "model")
        handle = await self.run_graph(graph, "nested")
        self.assertEqual((await self.node_output(handle.id, "merged"))["observed"], 7)
        self.assertEqual(self.model.calls, 1)

    async def test_nested_tool_receipt_and_completed_subgraph_survive_parent_reentry(self):
        import inspect
        effects = []
        @tool(side_effects="local")
        def record(value: int) -> dict:
            effects.append(value)
            return {"value": value}
        class Fixture:
            calls = 0
            async def complete(inner, model, messages, **options):
                inner.calls += 1
                calls = () if any(message["role"] == "tool" for message in messages) else ({"id": "record", "type": "function", "function": {"name": "record", "arguments": '{"value":7}'}},)
                return Completion(text="Verified" if not calls else "", tool_calls=calls, finish_reason="tool_calls" if calls else "stop", usage={"prompt_tokens": 20, "completion_tokens": 5})
        self.host.model_api = fixture = Fixture()
        self.host.register_tool(record)
        saved = await self.host.agents.get("agent")
        definition = AgentDefinition.model_validate(saved["definition"]).model_copy(update={"tools": ToolKit.local(files=False).with_tools(record).bindings.model_copy(update={"loading": "eager"})})
        await self.host.agents.save(definition, expected_revision=saved["revision"])
        graph = ArchitectureBuilder().node("nested", "subgraph", graph=react().model_dump(mode="json")).node("finish", "deliver").connect("nested", "finish")
        await self.host.architectures.create(AgentArchitecture(id="nested-recovery", project_id="p", name="Nested"), graph.build().model_dump(mode="json"))
        await self.host.architectures.publish("nested-recovery", "p", 1)
        await self.bind(ArchitectureBinding(architecture_id="nested-recovery", version=1))
        original_tools = RunContext.execute_tools
        async def tool_crash(context, state):
            await original_tools(context, state)
            raise RuntimeError("Tool receipt committed before its nested checkpoint")
        with patch.object(RunContext, "execute_tools", tool_crash):
            run = await self.submit("nested")
            self.assertEqual((await self.settle(run))["state"], "failed")
        self.assertEqual(effects, [7])
        self.assertEqual(fixture.calls, 1)
        original_call = self.host.store.call
        crashed = False
        async def parent_crash(function, **options):
            nonlocal crashed
            if inspect.isfunction(function) and function.__name__ == "save":
                closure = inspect.getclosurevars(function).nonlocals
                if not crashed and getattr(closure.get("node"), "id", None) == "nested":
                    crashed = True
                    raise RuntimeError("Nested graph completed before parent position")
            return await original_call(function, **options)
        with patch.object(self.host.store, "call", parent_crash):
            await self.host.runs.resume(run.id, "recover-tool")
            self.assertEqual((await self.settle(run))["state"], "failed")
        self.assertTrue(crashed)
        self.assertEqual(fixture.calls, 2)
        tool_steps = await self.host.architectures.inspect(run.id, "p", node_id="tools")
        self.assertEqual([row["state"] for row in tool_steps["items"]], ["completed"])
        await self.host.aclose()
        self.host = Host(Path(self.temp.name) / "state", model_api=fixture)
        self.host.register_tool(record)
        await self.host.__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.runs.resume(run.id, "recover-parent")
        self.assertEqual((await self.settle(run))["state"], "completed")
        self.assertEqual(effects, [7])
        self.assertEqual(fixture.calls, 2)

    async def test_nested_wait_resumes_the_original_child_interrupt(self):
        child = ArchitectureBuilder(react())
        child.value["edges"] = [edge for edge in child.value["edges"] if edge["source"] != "prepare"]
        child.connect("prepare", "wait")
        graph = ArchitectureBuilder().node("nested", "subgraph", graph=child.build().model_dump(mode="json")).node("finish", "deliver").connect("nested", "finish")
        await self.host.architectures.create(AgentArchitecture(id="nested-wait", project_id="p", name="Nested wait"), graph.build().model_dump(mode="json"))
        await self.host.architectures.publish("nested-wait", "p", 1)
        await self.bind(ArchitectureBinding(architecture_id="nested-wait", version=1))
        run = await self.submit("nested-wait")
        self.assertEqual((await self.settle(run))["state"], "waiting")
        await self.host.aclose()
        self.host = await Host(Path(self.temp.name) / "state", model_api=self.model).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.runs.resume(run.id, "reply", input="Continue")
        self.assertEqual((await self.settle(run))["state"], "completed")
        self.assertEqual(self.model.calls, 1)

    async def test_plan_replanning_and_reflection_use_saved_evidence(self):
        class Scripted:
            def __init__(self, values):
                self.values, self.messages = iter(values), []
            async def complete(inner, model, messages, **options):
                inner.messages.append(messages)
                return Completion(text=next(inner.values), usage={"prompt_tokens": 30, "completion_tokens": 5})
        plan = self.host.model_api = Scripted(["Plan A", "Step A complete", "Evidence requires REPLAN", "Plan B", "Verified PLAN_DONE"])
        handle = await self.run_graph(ArchitectureBuilder(templates()["plan_execute"]), "planning")
        self.assertEqual(len(plan.messages), 5)
        sources = await self.host.store.call(lambda db: rows(db, "SELECT origin FROM context_sources WHERE run_id=?", (handle.id,)))
        self.assertIn("architecture:plan", {row["origin"] for row in sources})
        reflection = self.host.model_api = Scripted(["First attempt", "Missing the requested evidence", "Lesson: verify the evidence", "Second attempt with evidence", "ACCEPT_RESULT"])
        handle = await self.run_graph(ArchitectureBuilder(templates()["reflexion"]), "reflection")
        self.assertEqual(len(reflection.messages), 5)
        self.assertIn("Missing the requested evidence", json.dumps(reflection.messages[3]))
        self.assertIn("Lesson: verify the evidence", json.dumps(reflection.messages[3]))
        self.assertEqual((await self.host.runs.result(handle.id)).text, "Second attempt with evidence")

    async def test_repeated_provider_call_ids_are_distinct_logical_actions_and_errors_repair(self):
        effects = []
        @tool(side_effects="local")
        def record(value: int) -> dict:
            effects.append(value)
            return {"recorded": value}
        class Scripted:
            calls = 0
            async def complete(inner, model, messages, **options):
                inner.calls += 1
                if inner.calls == 4:
                    return Completion(text="Corrected and completed", usage={"prompt_tokens": 20, "completion_tokens": 5})
                arguments = {"wrong": 1} if inner.calls == 1 else {"value": inner.calls}
                return Completion(tool_calls=({"id": "same-provider-id", "type": "function", "function": {"name": "record", "arguments": json.dumps(arguments)}},), finish_reason="tool_calls", usage={"prompt_tokens": 20, "completion_tokens": 5})
        self.host.model_api = Scripted()
        self.host.register_tool(record)
        saved = await self.host.agents.get("agent")
        definition = AgentDefinition.model_validate(saved["definition"]).model_copy(update={"tools": ToolKit.local(files=False).with_tools(record).bindings.model_copy(update={"loading": "eager"})})
        await self.host.agents.save(definition, expected_revision=saved["revision"])
        handle = await self.run_graph(ArchitectureBuilder(react()), "repair")
        self.assertEqual(effects, [2, 3])
        responses = await self.host.store.call(lambda db: rows(db, "SELECT body_json FROM conversation WHERE run_id=? AND role='tool'", (handle.id,)))
        self.assertEqual(len(responses), 3)

    async def test_current_revocation_and_model_node_scope_block_actual_tool(self):
        effects = []
        @tool(side_effects="local")
        def record(value: int) -> dict:
            effects.append(value)
            return {"recorded": value}
        class Scripted:
            calls = 0
            async def complete(inner, model, messages, **options):
                inner.calls += 1
                if inner.calls % 2 == 0:
                    return Completion(text="Permission correctly denied", usage={"prompt_tokens": 20, "completion_tokens": 5})
                return Completion(tool_calls=({"id": "blocked", "type": "function", "function": {"name": "record", "arguments": '{"value":1}'}},), finish_reason="tool_calls", usage={"prompt_tokens": 20, "completion_tokens": 5})
        self.host.model_api = Scripted()
        self.host.register_tool(record)
        saved = await self.host.agents.get("agent")
        definition = AgentDefinition.model_validate(saved["definition"]).model_copy(update={"tools": ToolKit.local(files=False).with_tools(record).bindings.model_copy(update={"loading": "eager"})})
        await self.host.agents.save(definition, expected_revision=saved["revision"])
        graph = ArchitectureBuilder(react())
        next(node for node in graph.value["nodes"] if node["id"] == "model")["policy"] = {"tools": {"allowed": []}}
        await self.run_graph(graph, "narrowed")
        self.assertEqual(effects, [])
        original = RunContext.execute_tools
        async def revoke(context, state):
            current = await self.host.agents.get("agent")
            current["definition"]["tools"]["functions"] = []
            await self.host.agents.save(AgentDefinition.model_validate(current["definition"]), expected_revision=current["revision"])
            return await original(context, state)
        with patch.object(RunContext, "execute_tools", revoke):
            await self.run_graph(ArchitectureBuilder(react()), "revoked")
        self.assertEqual(effects, [])

    async def test_sdk_precise_adjustment_and_independent_instances(self):
        client = ArchitectureClient(self.host)
        graph = templates()["reflexion"].model_dump(mode="json")
        graph["metadata"] = {"unshown": {"preserved": True}}
        await client.create(AgentArchitecture(id="sdk", project_id="p", name="SDK"), graph)
        from oea import ArchitectureOptions
        saved = await client.adjust("sdk", "p", 1, ArchitectureOptions(max_attempts=2))
        self.assertEqual(saved["draft"]["metadata"], graph["metadata"])
        version = await client.publish("sdk", "p", 2)
        instance = await self.host.instances.create("session", "agent", name="two")
        self.model.release.clear()
        first = await self.submit("parallel-first")
        second = await client.run("p", ArchitectureBinding(architecture_id="sdk", version=version["version"]), RunSpec(instance_id=instance["id"], input="Independent work", idempotency_key="parallel-second"))
        await asyncio.wait_for(self.model.entered.wait(), 5)
        self.model.release.set()
        self.assertEqual((await self.settle(first))["state"], "completed")
        await self.host.jobs[second["id"]]
        self.assertEqual((await self.host.runs.get(second["id"]))["state"], "completed")

    async def test_code_graph_registration_preview_source_and_capability_requirement(self):
        from oea.orchestration.graphs.default import compile_agent_graph
        self.host.register_graph("custom", compile_agent_graph, version="1", required_tools=("read_context",), source={"path": "agent_graph.py"})
        await self.host.architectures.create(AgentArchitecture(id="native", project_id="p", name="Native", representation="python"), {"graph": [], "graph_version": "1"})
        self.assertFalse((await self.host.architectures.validate("native", "p"))["valid"])
        await self.host.architectures.save("native", "p", 1, {"graph": "custom", "graph_version": "1"})
        await self.host.architectures.publish("native", "p", 2)
        preview = await self.host.architectures.preview("native", "p", version=1)
        self.assertTrue(preview["stages"])
        self.assertFalse(preview["editable"])
        self.assertEqual((await self.host.architectures.export("native", "p", version=1))["source"], {"path": "agent_graph.py"})
        await self.bind(ArchitectureBinding(architecture_id="native", version=1))
        handle = await self.submit("native")
        self.assertEqual((await self.settle(handle))["state"], "completed")
        self.host.register_graph("restricted", compile_agent_graph, version="1", required_tools=("unbound_tool",))
        await self.host.architectures.save("native", "p", 2, {"graph": "restricted", "graph_version": "1"})
        await self.host.architectures.publish("native", "p", 3)
        await self.bind(ArchitectureBinding(architecture_id="native", version=2))
        handle = await self.submit("missing-capability")
        self.assertEqual((await self.settle(handle))["error"]["code"], "REQUIRED_TOOL_UNAVAILABLE")

    async def test_manager_inherits_binding_and_blocks_main_switch_while_waiting(self):
        from oea import Manager
        saved = await self.host.agents.get("agent")
        definition = AgentDefinition.model_validate(saved["definition"]).model_copy(update={"manager": Manager(model=Model(base_url="http://model.invalid", model="test"))})
        await self.host.agents.save(definition, expected_revision=saved["revision"])
        first, second = await self.candidate(identity="first"), await self.candidate(identity="second")
        await self.bind(first)
        status = await self.host.architectures.status("agent", "p", role="manager")
        self.assertEqual(status["binding"], first.model_dump())
        await self.host.sessions.set_mode("session", "collaborative", 0)
        instance = await self.host.instances.create("session", "agent", name="coordinator", role="manager")
        self.model.release.clear()
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Work", idempotency_key="manager"))
        await asyncio.wait_for(self.model.entered.wait(), 5)
        with self.assertRaises(RuntimeFault) as blocked:
            await self.bind(second)
        self.assertEqual(blocked.exception.code, "AGENT_IN_USE")
        self.model.release.set()
        self.assertEqual((await self.settle(run))["state"], "completed")


class StaticArchitectureTests(unittest.TestCase):
    def test_structure_and_reducer_diagnostics_are_localized(self):
        graph = react().model_dump(mode="json")
        graph["edges"][0]["target"] = "missing"
        _, errors, _ = validate_graph(graph)
        self.assertIn(["edges", 0], [error["path"] for error in errors])
        graph = react().model_dump(mode="json")
        graph["state"] = {"counter": {"type": "string", "reducer": "sum"}}
        _, errors, _ = validate_graph(graph)
        self.assertIn(["state", "counter", "reducer"], [error["path"] for error in errors])
