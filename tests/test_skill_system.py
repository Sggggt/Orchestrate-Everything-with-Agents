"""Shared Skill packages and run policies using temporary synthetic inputs."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from agent.llm import LLMApiAgent
from ma_runtime import AgentConfig, MCPBinding, MCPServerConfig, PlatformClient, Runtime, SkillBinding, UsePolicy
from ma_runtime.bridges.compact import CompactBridge
from ma_runtime.errors import RuntimeFault
from ma_runtime.mcp_service import tool_alias
from ma_runtime.skill_registry import read_directory
from ma_runtime.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def package(root, name="review", body="Check actual evidence."):
    directory = root / name
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review results using evidence.\nmetadata:\n  version: '1'\n---\n\n# Review\n{body}\n",
        encoding="utf-8")
    (directory / "references").mkdir(exist_ok=True)
    (directory / "references/check.md").write_text("# Checks\nInspect receipts.", encoding="utf-8")
    return directory


class PolicyLLM(LLMApiAgent):
    def __init__(self, config, actions=()):
        super().__init__(config)
        self.actions, self.captured = list(actions), []

    async def _completion(self, messages, context, tools=None):
        self.captured.append({"messages": list(messages), "tools": tools, "choice": context.model_tool_choice})
        calls = []
        if self.actions:
            name, arguments = self.actions.pop(0)
            calls = [{"id": f"call-{len(self.captured)}", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(arguments)}}]
        yield {"calls": calls, "text": "Finished" if not calls else "", "finish_reason": "stop"}


class SkillSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-skills-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        self.directory = package(self.root)
        await self.rt.skills.register_directory(str(self.directory))

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def agent(self, actions=(), **options):
        config = AgentConfig(id="worker", name="Worker", kind="llm", base_url="http://model.invalid/v1",
                             model="scripted", skill_bindings=(SkillBinding(skill_id="review"),), **options)
        await self.rt.agents.configure(config)
        adapter = PolicyLLM(config, actions)
        self.rt.adapters[config.id] = adapter
        session = await self.rt.sessions.create(title="Policy contract")
        thread = await self.rt.threads.create(session.id, config.id)
        return adapter, thread

    async def mcp(self):
        await self.rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
            args=("-B", str(ROOT / "tests/fixtures/mcp_server.py"))))

    async def test_registry_refresh_snapshot_and_unlink_guard(self):
        original = await self.rt.skills.get("review")
        await self.agent()
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.skills.delete("review")
        self.assertEqual(error.exception.code, "SKILL_IN_USE")
        package(self.root, body="Updated checklist.")
        self.assertNotIn("Updated", (await self.rt.skills.read("review"))["content"])
        refreshed = await self.rt.skills.refresh("review")
        self.assertNotEqual(original["revision"], refreshed["revision"])
        self.assertIn("Updated", (await self.rt.skills.read("review"))["content"])
        with self.assertRaises(RuntimeFault):
            await self.rt.skills.read("review", "../outside.md")
        self.assertEqual((await self.rt.skills.read("review", "references/check.md"))["content"], "# Checks\nInspect receipts.")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        self.assertEqual((await self.rt.skills.get("review"))["revision"], refreshed["revision"])

    async def test_standard_specification_and_directory_conflicts(self):
        for content in ("# Missing metadata", "---\nname: wrong\ndescription: Review\n---\n",
                        "---\nname: review\ndescription: ''\n---\n",
                        "---\nname: review\ndescription: Review\nmetadata: {version: 1}\n---\n"):
            with self.subTest(content=content):
                (self.directory / "SKILL.md").write_text(content, encoding="utf-8")
                with self.assertRaises(RuntimeFault):
                    read_directory(str(self.directory))
        other = self.root / "other"
        other.mkdir()
        directory = package(other)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.skills.register_directory(str(directory))
        self.assertEqual(error.exception.code, "SKILL_CONFLICT")

    async def test_summary_discovery_and_default_auto(self):
        adapter, thread = await self.agent()
        capabilities = await self.rt.agents.capabilities("worker")
        self.assertEqual(capabilities["skills"][0]["id"], "review")
        self.assertNotIn("content", capabilities["skills"][0])
        task = await self.rt.threads.turn(thread.id, "Review", "auto")
        await self.rt.tasks.result(task.id, timeout=10)
        prompt = adapter.captured[0]["messages"][0]["content"]
        self.assertNotIn("Check actual evidence.", prompt)
        self.assertIsNone(adapter.captured[0]["choice"])

    async def test_required_skill_is_preloaded_before_model_call(self):
        adapter, thread = await self.agent()
        task = await self.rt.threads.turn(thread.id, "Review", "preload", skill_policy={"required": ["review"]})
        await self.rt.tasks.result(task.id, timeout=10)
        self.assertIn("Check actual evidence.", adapter.captured[0]["messages"][0]["content"])
        self.assertIsNone(adapter.captured[0]["choice"])

    async def test_required_any_skill_forces_reader_and_checks_evidence(self):
        adapter, thread = await self.agent(actions=(("read_skill", {"skill_id": "review"}),))
        task = await self.rt.threads.turn(thread.id, "Review", "read-any", skill_policy={"mode": "required"})
        await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(adapter.captured[0]["choice"], "required")
        self.assertIsNone(adapter.captured[1]["choice"])

    async def test_required_tool_success_and_forced_choice_release(self):
        await self.mcp()
        alias = tool_alias("fixture", "echo")
        adapter, thread = await self.agent(actions=((alias, {"text": "checked"}),),
                                          mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),))
        task = await self.rt.threads.turn(thread.id, "Verify", "tool-success",
            tool_policy={"allowed": ["mcp:fixture:echo"], "required": ["mcp:fixture:echo"]},
            skill_policy={"required": ["review"]})
        await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(adapter.captured[0]["choice"], "required")
        self.assertIsNone(adapter.captured[1]["choice"])
        calls = await self.rt.mcp.calls("fixture")
        self.assertEqual(len(calls), 1)

    async def test_text_response_cannot_satisfy_required_tool(self):
        _, thread = await self.agent()
        task = await self.rt.threads.turn(thread.id, "Verify", "missing-tool", tool_policy={"required": ["list_threads"]})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        state = await self.rt.tasks.get(task.id)
        self.assertEqual(state.state, "FAILED")
        self.assertEqual(state.error["code"], "EXECUTION_REQUIREMENT_UNMET")

    async def test_required_target_keeps_prerequisite_tools_available(self):
        await self.mcp()
        actions = ((tool_alias("fixture", "select_project"), {"project": "selected"}),
                   (tool_alias("fixture", "echo"), {"text": "checked"}))
        adapter, thread = await self.agent(actions=actions, mcp_bindings=(MCPBinding(server_id="fixture"),))
        task = await self.rt.threads.turn(thread.id, "Select then read", "prerequisites", tool_policy={
            "allowed": ["mcp:fixture:select_project", "mcp:fixture:echo"], "required": ["mcp:fixture:echo"]})
        await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(adapter.captured[0]["choice"], "required")
        self.assertEqual(adapter.captured[1]["choice"], "required")
        self.assertIsNone(adapter.captured[2]["choice"])
        self.assertEqual(len(await self.rt.mcp.calls("fixture")), 2)

    async def test_upstream_error_cannot_satisfy_required_tool(self):
        await self.mcp()
        adapter, thread = await self.agent(actions=((tool_alias("fixture", "echo"), {"text": "checked"}),),
            mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),))
        with patch.object(self.rt.mcp, "_call", AsyncMock(return_value={"isError": True, "content": []})):
            task = await self.rt.threads.turn(thread.id, "Inspect", "upstream-error",
                tool_policy={"required": ["mcp:fixture:echo"]})
            with self.assertRaises(RuntimeFault):
                await self.rt.tasks.result(task.id, timeout=10)
        state = await self.rt.tasks.get(task.id)
        self.assertEqual(state.state, "FAILED")
        self.assertFalse(any(e["kind"] == "tool" for e in state.execution_evidence))

    async def test_uncertain_tool_stops_required_loop_without_replay(self):
        await self.mcp()
        alias = tool_alias("fixture", "increment")
        adapter, thread = await self.agent(actions=((alias, {}), (alias, {})),
            mcp_bindings=(MCPBinding(server_id="fixture", tools=("increment",)),))
        operation = AsyncMock(side_effect=RuntimeFault("OUTCOME_UNCERTAIN", "Verify the original action"))
        with patch.object(self.rt.mcp, "_call", operation):
            task = await self.rt.threads.turn(thread.id, "Inspect", "uncertain-policy",
                tool_policy={"required": ["mcp:fixture:increment"]})
            with self.assertRaises(RuntimeFault):
                await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(operation.await_count, 1)
        self.assertEqual(len(adapter.captured), 1)
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "UNCERTAIN")

    async def test_evidence_survives_suspend_and_host_restart(self):
        _, thread = await self.agent(actions=(("list_threads", {}), ("wait_for_input", {})))
        task = await self.rt.threads.turn(thread.id, "Inspect and wait", "durable-policy",
            tool_policy={"required": ["list_threads"]}, skill_policy={"required": ["review"]})
        for _ in range(200):
            state = await self.rt.tasks.get(task.id)
            if state.state == "WAITING":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(state.state, "WAITING")
        self.assertEqual({e["name"] for e in state.execution_evidence}, {"review", "list_threads"})
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        adapter = PolicyLLM(self.rt.agent_configs["worker"])
        self.rt.adapters["worker"] = adapter
        await self.rt.tasks.resume(task.id, {"wait_token": state.wait.wait_token, "input": "Continue"}, "resume-policy")
        await self.rt.tasks.result(task.id, timeout=10)
        self.assertIsNone(adapter.captured[0]["choice"])
        state = await self.rt.tasks.get(task.id)
        self.assertEqual(len(state.execution_evidence), 2)

    async def test_none_hides_tools_and_skills(self):
        await self.mcp()
        adapter, thread = await self.agent(mcp_bindings=(MCPBinding(server_id="fixture"),))
        task = await self.rt.threads.turn(thread.id, "Answer", "none", tool_policy={"mode": "none"}, skill_policy={"mode": "none"})
        await self.rt.tasks.result(task.id, timeout=10)
        names = [t["function"]["name"] for t in adapter.captured[0]["tools"]]
        self.assertNotIn("read_skill", names)
        self.assertNotIn("list_threads", names)
        self.assertFalse(any(name.startswith("mcp_") for name in names))
        self.assertIn("wait_for_input", names)
        self.assertEqual((await self.rt.mcp.get("fixture"))["connections"], 0)

    async def test_policy_cannot_grant_unlinked_capabilities(self):
        _, thread = await self.agent()
        for changes in ({"tool_policy": {"allowed": ["mcp:missing:echo"]}},
                        {"skill_policy": {"required": ["missing"]}}):
            with self.assertRaises(RuntimeFault):
                await self.rt.threads.turn(thread.id, "Answer", "invalid", **changes)
        with self.assertRaises(ValueError):
            UsePolicy(mode="none", required=("review",))

    async def test_default_policy_preserves_legacy_idempotency_hash(self):
        _, thread = await self.agent()
        task = await self.rt.threads.turn(thread.id, "Answer", "legacy-key")
        await self.rt.tasks.result(task.id, timeout=10)
        state = await self.rt.tasks.get(task.id)
        from ma_runtime.models import content_hash
        legacy = state.spec.model_dump(mode="json", exclude={"tool_policy", "skill_policy", "result_policy", "context_budget_tokens", "context_refs", "continuation_policy"})
        self.assertEqual(state.request_hash, content_hash(legacy))
        repeated = await self.rt.threads.turn(thread.id, "Answer", "legacy-key")
        self.assertTrue(repeated.reused)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.threads.turn(thread.id, "Answer", "legacy-key", tool_policy={"mode": "none"})
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_batch_propagates_policies_to_each_independent_task(self):
        _, thread = await self.agent()
        batch = await self.rt.instances.spawn({"session_id": thread.session_id, "agent_ids": ["worker"],
            "payloads": [{"input": "First"}, {"input": "Second"}], "idempotency_key": "policies-batch",
            "tool_policy": {"mode": "none"}, "skill_policy": {"required": ["review"]}})
        result = await self.rt.instances.result(batch["id"], timeout=10)
        for member in result["instances"]:
            task = await self.rt.tasks.get(member["task_id"])
            self.assertEqual(task.spec.tool_policy.mode, "none")
            self.assertEqual(task.spec.skill_policy.required, ("review",))
            self.assertEqual(task.execution_evidence[0]["name"], "review")

    async def test_default_workflow_policies_preserve_creation_idempotency(self):
        await self.agent()
        from ma_runtime import WorkflowBuilder
        from ma_runtime.models import content_hash
        from ma_runtime.store import one
        graph = (WorkflowBuilder().node("start", "start").node("execute", "agent", agent_id="worker")
            .node("end", "end").connect("start", "execute").connect("execute", "end").build())
        saved = await self.rt.workflows.create("default", "Legacy definition", "legacy-definition", graph=graph)
        legacy = graph.model_dump(mode="json")
        for node in legacy["nodes"]:
            node["config"].pop("tool_policy", None)
            node["config"].pop("skill_policy", None)
            node["config"].pop("result_policy", None)
            node["config"].pop("context_budget_tokens", None)
            node["config"].pop("context_refs", None)
            node["config"].pop("continuation_policy", None)
        expected = content_hash({"name": "Legacy definition", "description": "", "graph": legacy})
        row = await self.rt.store.call(lambda db: one(db, "SELECT request_hash FROM workflows WHERE id=?", (saved.id,)))
        self.assertEqual(row.request_hash, expected)
        self.assertEqual((await self.rt.workflows.create("default", "Legacy definition", "legacy-definition", graph=graph)).id, saved.id)

    async def test_agent_and_map_workflows_propagate_execution_policies(self):
        await self.agent()
        from ma_runtime import WorkflowBuilder
        for kind in ("agent", "map"):
            options = {"agent_id": "worker"} if kind == "agent" else {"agent_ids": ["worker"], "items": "{{inputs.items}}"}
            graph = (WorkflowBuilder().node("start", "start").node("execute", kind, **options,
                tool_policy={"mode": "none"}, skill_policy={"required": ["review"]})
                .node("end", "end", outputs={"done": True}).connect("start", "execute").connect("execute", "end").build())
            workflow = await self.rt.workflows.create("default", "Policy " + kind, "definition-" + kind, graph=graph)
            run = await self.rt.workflows.run(workflow.id, {"input": "Review", "items": ["First", "Second"]}, "run-" + kind)
            await self.rt.workflows.result(run.id, timeout=10)
            tasks = await self.rt.tasks.list(run.session_id)
            self.assertEqual(len(tasks.items), 1 if kind == "agent" else 2)
            for task in tasks.items:
                self.assertEqual(task.spec.tool_policy.mode, "none")
                self.assertEqual(task.spec.skill_policy.required, ("review",))

    async def test_disabled_skill_rejects_required_load(self):
        _, thread = await self.agent()
        await self.rt.skills.set_enabled("review", False)
        with self.assertRaises(RuntimeFault):
            await self.rt.threads.turn(thread.id, "Review", "disabled", skill_policy={"required": ["review"]})
        self.assertFalse((await self.rt.agents.capabilities("worker"))["skills"][0]["enabled"])

    async def test_skill_revoked_while_queued_fails_before_starting_process(self):
        config = AgentConfig(id="integration-inspector", name="Inspector", kind="process", llm_required=True,
            command=(sys.executable, "-B", str(ROOT / "examples/integration_agent.py")),
            skill_bindings=(SkillBinding(skill_id="review"),))
        await self.rt.agents.configure(config)
        session = await self.rt.sessions.create(title="Queued revocation")
        thread = await self.rt.threads.create(session.id, config.id)
        task = await self.rt.threads.turn(thread.id, "Inspect", "queued-revocation", skill_policy={"required": ["review"]})
        await self.rt.skills.set_enabled("review", False)
        await self.rt.agents.configure(config.model_copy(update={"llm_required": False}))
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        state = await self.rt.tasks.get(task.id)
        self.assertEqual(state.state, "FAILED")
        self.assertEqual(state.error["code"], "PERMISSION_DENIED")
        self.assertEqual(self.rt.adapters[config.id].finished, {})

    async def test_skill_none_blocks_run_scoped_reader(self):
        _, thread = await self.agent()
        class Checking(PolicyLLM):
            async def _completion(inner, messages, context, tools=None):
                self.assertEqual(await context.list_skills(), [])
                with self.assertRaises(RuntimeFault) as error:
                    await context.read_skill("review")
                self.assertEqual(error.exception.code, "PERMISSION_DENIED")
                yield {"calls": [], "text": "Checked", "finish_reason": "stop"}
        self.rt.adapters["worker"] = Checking(self.rt.agent_configs["worker"])
        task = await self.rt.threads.turn(thread.id, "Inspect", "no-skill-reader", skill_policy={"mode": "none"})
        await self.rt.tasks.result(task.id, timeout=10)

    async def test_snapshot_survives_refresh_and_current_revocation_blocks_read(self):
        _, thread = await self.agent()
        # Use an actual running context to verify revocation independently of
        # model selection and the adapter's cached function catalog.
        class Revoking(PolicyLLM):
            async def _completion(inner, messages, context, tools=None):
                package(self.root, body="New version.")
                await self.rt.skills.refresh("review")
                value = await context.read_skill("review")
                self.assertIn("Check actual evidence", value["content"])
                await self.rt.skills.set_enabled("review", False)
                with self.assertRaises(RuntimeFault):
                    await context.read_skill("review")
                yield {"calls": [], "text": "Verified", "finish_reason": "stop"}
        self.rt.adapters["worker"] = Revoking(self.rt.agent_configs["worker"])
        task = await self.rt.threads.turn(thread.id, "Inspect", "revocation")
        await self.rt.tasks.result(task.id, timeout=10)

    async def test_process_agent_uses_shared_skill_and_required_tool(self):
        await self.mcp()
        config = AgentConfig(id="integration-inspector", name="Inspector", kind="process",
            command=(sys.executable, "-B", str(ROOT / "examples/integration_agent.py")),
            skill_bindings=(SkillBinding(skill_id="review"),), mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),))
        await self.rt.agents.configure(config)
        session = await self.rt.sessions.create(title="Process policy")
        thread = await self.rt.threads.create(session.id, config.id)
        task = await self.rt.tasks.submit({"session_id": session.id, "thread_id": thread.id, "agent_id": config.id,
            "payload": {"input": "Inspect", "tool": tool_alias("fixture", "echo"), "arguments": {"text": "checked"}},
            "idempotency_key": "process-policy", "skill_policy": {"required": ["review"]},
            "tool_policy": {"required": ["mcp:fixture:echo"]}})
        result = await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(result.output["skill_loaded"], "skills/review/SKILL.md")

    async def test_process_sdk_cannot_invoke_tool_disabled_by_task_policy(self):
        await self.mcp()
        config = AgentConfig(id="integration-inspector", name="Inspector", kind="process",
            command=(sys.executable, "-B", str(ROOT / "examples/integration_agent.py")),
            mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),))
        await self.rt.agents.configure(config)
        session = await self.rt.sessions.create(title="Denied process tool")
        thread = await self.rt.threads.create(session.id, config.id)
        task = await self.rt.tasks.submit({"session_id": session.id, "thread_id": thread.id, "agent_id": config.id,
            "payload": {"tool": tool_alias("fixture", "echo"), "arguments": {"text": "blocked"}},
            "idempotency_key": "denied-process", "tool_policy": {"mode": "none"}})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        # Default process descriptors have partial mediation; preserve their
        # conservative outcome contract even though this SDK call was denied.
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "UNCERTAIN")
        self.assertEqual(await self.rt.mcp.calls("fixture"), [])

    async def test_public_skill_source_inside_checkout_is_accepted(self):
        source = ROOT / "integrations/codex/orchestrate-everything-with-agents"
        value = await self.rt.skills.register_directory(str(source))
        self.assertEqual(value["id"], "orchestrate-everything-with-agents")
        self.assertGreater(value["resource_count"], 0)

    async def test_directory_mcp_registration_preserves_source(self):
        directory = self.root / "mcp"
        directory.mkdir()
        source = directory / "mcp.json"
        source.write_text(json.dumps({"id": "directory", "name": "Directory", "command": sys.executable,
            "args": ["-B", str(ROOT / "tests/fixtures/mcp_server.py")]}), encoding="utf-8")
        await self.rt.mcp.register_directory(str(directory))
        self.assertEqual(self.rt.mcp.configs["directory"].cwd, str(directory.resolve()))
        await self.rt.mcp.delete("directory")
        self.assertTrue(source.is_file())

    async def test_sdk_http_compact_discover_same_registry(self):
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                self.assertEqual((await client.skills.list())["items"][0]["id"], "review")
                self.assertIn("Inspect receipts", (await client.skills.read("review", "references/check.md"))["content"])
                self.assertIn("skills.register_directory", (await client.protocol())["methods"])
        async def invoke(method, params):
            from ma_runtime.bridges.dispatch import dispatch
            return {"result": await dispatch(self.rt, method, params)}
        bridge = CompactBridge(invoke)
        guide = bridge.guide("skills", "register_directory")
        self.assertIn("directory", [p["name"] for p in guide["parameters"]])
        self.assertIn("TaskSpec", bridge.guide("threads", "turn")["schemas"])
