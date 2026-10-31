"""Deferred schemas retain native calls, current grants, and durable evidence."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Completion, Model, RunSpec, Tools
from oea.contracts.configuration import CapabilityUse, UsePolicy
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.tools.contracts import ToolSpec


def call(identity, name, arguments):
    return Completion(tool_calls=({"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}},), finish_reason="tool_calls",
        usage={"prompt_tokens": 20, "completion_tokens": 4})


class BusinessTool:
    def __init__(self, name):
        self.calls = []
        self.spec = ToolSpec(name=name, description="Calculate a bounded result.", input_schema={"type": "object",
            "properties": {"value": {"type": "integer", "description": "PARAMETER_DETAIL " * 100}}, "required": ["value"], "additionalProperties": False})

    def describe(self):
        return self.spec

    async def invoke(self, request):
        self.calls.append(request["arguments"])
        return {"value": request["arguments"]["value"] + 1}


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-discovery-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state").__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.session = await self.host.sessions.create("p")
        self.business = BusinessTool("calculate")
        self.host.register_tool(self.business)
        await self.save()

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def save(self, *, loading="deferred", functions=("calculate",), schema_budget_tokens=None, stalled=12):
        from oea import Budget, Execution
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="fixture"),
            execution=Execution(budget=Budget(max_stalled_model_calls=stalled)),
            tools=Tools(loading=loading, functions=functions, schema_budget_tokens=schema_budget_tokens)))

    async def test_soft_target_protects_one_request_then_unloads_and_allows_reload(self):
        await self.save(schema_budget_tokens=1)
        seen = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                seen.append({item["function"]["name"] for item in options["tools"]})
                if len(seen) in {1, 3}:
                    return call("load-" + str(len(seen)), "tool_guide", {"name": "calculate"})
                if len(seen) == 2:
                    return call("directory", "tool_guide", {"query": "calculate"})
                if len(seen) == 4:
                    return call("use", "calculate", {"value": 7})
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("calculate", seen[1])
        self.assertNotIn("calculate", seen[2])
        self.assertIn("calculate", seen[3])
        self.assertEqual(self.business.calls, [{"value": 7}])
        unloads = await self.host.store.call(lambda db: rows(db, "SELECT data_json FROM events WHERE run_id=? AND kind='tool.schema_unloaded'", (run.id,)))
        self.assertTrue(any(json.loads(row["data_json"])["reason"] == "schema_budget" for row in unloads))

    async def test_pin_preserves_large_schema_and_is_persisted(self):
        await self.save(schema_budget_tokens=1)
        seen = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                seen.append({item["function"]["name"] for item in options["tools"]})
                if len(seen) == 1:
                    return call("load", "tool_guide", {"name": "calculate", "pin": True})
                if len(seen) < 5:
                    return call("directory-" + str(len(seen)), "tool_guide", {"query": "calculate"})
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=5)
        self.assertTrue(all("calculate" in names for names in seen[1:]))
        state = await self.host.store.call(lambda db: one(db, "SELECT * FROM run_tool_state WHERE run_id=? AND name='calculate'", (run.id,)))
        self.assertEqual((state["loaded"], state["pinned"]), (1, 1))

    async def test_recently_used_single_large_schema_keeps_its_next_opportunity(self):
        await self.save(schema_budget_tokens=1)
        seen = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                seen.append({item["function"]["name"] for item in options["tools"]})
                if len(seen) == 1:
                    return call("load", "tool_guide", {"name": "calculate"})
                if len(seen) == 2:
                    return call("use", "calculate", {"value": 3})
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("calculate", seen[-1])
        self.assertEqual(self.business.calls, [{"value": 3}])

    async def test_recovery_keeps_the_saved_excerpt_after_projection_policy_changes(self):
        from oea.runtime.context import RunContext
        await self.save(loading="eager")
        async def invoke(request):
            self.business.calls.append(request["arguments"])
            return {"text": "Original evidence. " * 2000}
        self.business.invoke = invoke
        requests = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                requests.append(1)
                if len(requests) == 1:
                    return call("use", "calculate", {"value": 7})
                self.assertIn("Prior excerpt", json.dumps(messages))
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        original = RunContext.execute_tools
        async def interrupted(context, state):
            await original(context, state)
            raise RuntimeFault("TEST_INTERRUPTED", "Stopped after the saved observation")
        with patch.object(RunContext, "execute_tools", interrupted), patch("oea.context.store.prune_text", return_value="Prior excerpt"):
            run = await self.submit()
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(run.id, timeout=5)
        await self.host.runs.resume(run.id, "continue")
        self.assertEqual((await self.host.runs.result(run.id, timeout=5)).text, "Done")
        self.assertEqual(self.business.calls, [{"value": 7}])

    async def test_contract_matches_sent_catalog_when_registration_changes_during_preparation(self):
        from oea.runtime.context import RunContext
        import hashlib
        await self.save(loading="eager")
        original = RunContext.instructions
        expected = self.host.tools.contracts["calculate"][0]
        replacement = BusinessTool("calculate")
        replacement.spec.description = "Changed contract description."
        changed, requests = False, []
        async def instructions(context):
            nonlocal changed
            value = await original(context)
            if not changed:
                changed = True
                self.host.tools.providers.pop("calculate")
                self.host.register_tool(replacement)
            return value
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                requests.append(1)
                if len(requests) == 1:
                    return call("use", "calculate", {"value": 7})
                self.assertIn("TOOL_CONTRACT_CHANGED", json.dumps(messages))
                return Completion(text="Changed contract requires a new call", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        with patch.object(RunContext, "instructions", instructions):
            run = await self.submit()
            await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(replacement.calls, [])
        contract = await self.host.store.call(lambda db: one(db, "SELECT spec_json FROM tool_contracts WHERE id=?", (expected,)))
        self.assertEqual(hashlib.sha256(contract["spec_json"].encode()).hexdigest(), expected)

    async def test_protected_schema_over_hard_input_budget_stops_before_send(self):
        self.host.tools.providers.pop("calculate")
        huge = BusinessTool("calculate")
        huge.spec.input_schema["properties"]["value"]["description"] = "Parameter. " * 20000
        self.host.register_tool(huge)
        calls = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                calls.append(1)
                return call("load", "tool_guide", {"name": "calculate", "pin": True})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(caught.exception.code, "MODEL_CONTEXT_BUDGET")
        self.assertEqual(len(calls), 1)
        self.assertFalse(huge.calls)

    async def test_automatic_unload_stays_unloaded_after_restart(self):
        await self.save(schema_budget_tokens=1)
        observed = []
        class InitialModel:
            async def complete(inner, model, messages, **options):
                observed.append({item["function"]["name"] for item in options["tools"]})
                if len(observed) == 1:
                    return call("load", "tool_guide", {"name": "calculate"})
                if len(observed) == 2:
                    return call("search", "tool_guide", {"query": "calculate"})
                return call("wait", "wait_for_input", {"prompt": "Continue"})
        self.host.model_api = InitialModel()
        run = await self.submit()
        async with asyncio.timeout(5):
            while True:
                changed = self.host.run_signal(run.id)
                if (await self.host.runs.get(run.id))["state"] == "waiting":
                    break
                await changed.wait()
        self.assertNotIn("calculate", observed[-1])
        await self.host.aclose()
        class ResumedModel:
            async def complete(inner, model, messages, **options):
                self.assertNotIn("calculate", {item["function"]["name"] for item in options["tools"]})
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host = await Host(self.root / "state", model_api=ResumedModel()).__aenter__()
        self.host.register_tool(self.business)
        await self.host.runs.resume(run.id, "continue", input="Continue")
        await self.host.runs.result(run.id, timeout=5)

    async def test_changed_contract_is_rejected_before_provider_execution(self):
        await self.save(loading="eager")
        count = 0
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                nonlocal count
                count += 1
                if count == 1:
                    self.host.tools.providers.pop("calculate")
                    changed = BusinessTool("calculate")
                    changed.spec.description = "A changed contract"
                    self.host.register_tool(changed)
                    return call("changed", "calculate", {"value": 2})
                self.assertIn("TOOL_CONTRACT_CHANGED", json.dumps(messages))
                return Completion(text="Reviewed", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(self.host.tools.providers["calculate"].calls, [])
        self.assertEqual(self.business.calls, [])

    async def test_repeated_validation_failure_links_no_progress_to_original_rejection(self):
        await self.save(loading="eager", stalled=2)
        count = 0
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                nonlocal count
                count += 1
                return call("invalid-" + str(count), "calculate", {"value": "private-input"})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(caught.exception.code, "NO_PROGRESS")
        self.assertEqual(caught.exception.identity["cause"]["code"], "INVALID_REQUEST")
        inspection = await self.host.runs.inspect(run.id)
        self.assertEqual(len(inspection["rejections"]), 2)
        self.assertEqual(inspection["activity"]["blocker_repeats"], 2)
        self.assertEqual(inspection["budget"]["usage"]["tool_calls"], 0)
        self.assertNotIn("private-input", json.dumps(inspection))
        self.assertEqual(self.business.calls, [])

    async def test_rejected_call_replay_after_checkpoint_failure_does_not_emit_twice(self):
        await self.save(loading="eager")
        count = 0
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                nonlocal count
                count += 1
                return call("invalid", "calculate", {"value": "wrong"}) if count == 1 else Completion(text="Reviewed", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        original = self.host.contexts.tool_result
        async def crash_after(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeFault("TEST_INTERRUPTED", "Stopped after saving the tool result")
        with patch.object(self.host.contexts, "tool_result", crash_after):
            run = await self.submit()
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(run.id, timeout=5)
        await self.host.runs.resume(run.id, "resume")
        await self.host.runs.result(run.id, timeout=5)
        inspection = await self.host.runs.inspect(run.id)
        self.assertEqual(len(inspection["rejections"]), 1)
        self.assertEqual(self.business.calls, [])

    async def submit(self, key="work", policy=None):
        instance = await self.host.instances.create(self.session["id"], "worker", name="Worker")
        return await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Calculate", idempotency_key=key,
                                                 policy=policy or UsePolicy()))

    async def test_search_then_load_exposes_only_selected_schema_and_calls_original_tool(self):
        host, observed, responses = self.host, [], []
        class ModelFixture:
            async def complete(self, model, messages, **options):
                observed.append(options["tools"])
                responses.extend(json.loads(message["content"]) for message in messages if message["role"] == "tool")
                step = len(observed)
                if step == 1:
                    return call("find", "tool_guide", {"query": "calculate", "limit": 1})
                if step == 2:
                    return call("load", "tool_guide", {"name": "calculate"})
                if step == 3:
                    return call("execute", "calculate", {"value": 7})
                return Completion(text="Eight", usage={"prompt_tokens": 20, "completion_tokens": 2})
        host.model_api = ModelFixture()
        policy = UsePolicy(tools=CapabilityUse(allowed=("calculate",), required=("calculate",)))
        run = await self.submit(policy=policy)
        self.assertEqual((await host.runs.result(run.id, timeout=5)).text, "Eight")
        names = lambda tools: {tool["function"]["name"] for tool in tools}
        self.assertEqual(names(observed[0]), {"tool_guide"})
        self.assertNotIn("PARAMETER_DETAIL", json.dumps(observed[:2]))
        self.assertIn("calculate", names(observed[2]))
        self.assertIn("PARAMETER_DETAIL", json.dumps(observed[2]))
        self.assertEqual(self.business.calls, [{"value": 7}])
        self.assertTrue(any(value.get("loaded") for value in responses))
        actions = await host.store.call(lambda db: rows(db, "SELECT name FROM actions WHERE run_id=? AND state='completed'", (run.id,)))
        self.assertIn("calculate", [item["name"] for item in actions])
        self.assertNotIn("tool_call", [item["name"] for item in actions])

    async def test_loaded_schema_is_removed_when_binding_is_revoked(self):
        host, seen = self.host, []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                seen.append(options["tools"])
                if len(seen) == 1:
                    return call("load", "tool_guide", {"name": "calculate"})
                if len(seen) == 2:
                    await self.save(functions=())
                    return call("execute", "calculate", {"value": 2})
                self.assertIn("PERMISSION_DENIED", json.dumps(messages))
                return Completion(text="Revoked", usage={"prompt_tokens": 20, "completion_tokens": 2})
        host.model_api = ModelFixture()
        run = await self.submit()
        await host.runs.result(run.id, timeout=5)
        self.assertEqual(self.business.calls, [])
        self.assertNotIn("calculate", [tool["function"]["name"] for tool in seen[-1]])

    async def test_selection_restores_from_receipt_after_restart_and_is_scoped_to_run(self):
        host, observed = self.host, []
        class InitialModel:
            async def complete(inner, model, messages, **options):
                observed.append(options["tools"])
                return call("load", "tool_guide", {"name": "calculate"}) if len(observed) == 1 else call("wait", "wait_for_input", {"prompt": "Continue"})
        host.model_api = InitialModel()
        run = await self.submit()
        async with asyncio.timeout(5):
            while True:
                changed = host.run_signal(run.id)
                if (await host.runs.get(run.id))["state"] == "waiting":
                    break
                await changed.wait()
        await host.aclose()
        class ResumedModel:
            async def complete(inner, model, messages, **options):
                observed.append(options["tools"])
                return Completion(text="Complete", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host = await Host(self.root / "state", model_api=ResumedModel()).__aenter__()
        self.host.register_tool(self.business)
        await self.host.runs.resume(run.id, "resume", input="Continue", refresh_capabilities=False)
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("calculate", [tool["function"]["name"] for tool in observed[-1]])
        fresh = await self.submit("fresh")
        await self.host.runs.result(fresh.id, timeout=5)
        self.assertNotIn("calculate", [tool["function"]["name"] for tool in observed[-1]])

    async def test_eager_mode_and_invalid_parameters_preserve_existing_contract(self):
        await self.save(loading="eager")
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                self.assertIn("PARAMETER_DETAIL", json.dumps(options["tools"]))
                if not any(message["role"] == "tool" for message in messages):
                    return call("invalid", "calculate", {"value": "wrong"})
                self.assertIn("INVALID_REQUEST", json.dumps(messages))
                return Completion(text="Checked", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(self.business.calls, [])

    async def test_unload_removes_the_schema_without_executing_the_tool(self):
        observed = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                observed.append({item["function"]["name"] for item in options["tools"]})
                if len(observed) == 1:
                    return call("load", "tool_guide", {"name": "calculate"})
                if len(observed) == 2:
                    return call("unload", "tool_guide", {"name": "calculate", "load": False})
                return Completion(text="Done", usage={"prompt_tokens": 20, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=5)
        self.assertNotIn("calculate", observed[0])
        self.assertIn("calculate", observed[1])
        self.assertNotIn("calculate", observed[2])
        self.assertEqual(self.business.calls, [])

    async def test_secret_in_a_deferred_parameter_is_rejected_before_its_schema_is_sent(self):
        secret = "fixture-protected-discovery-key"
        self.business.spec.input_schema["properties"]["value"]["description"] = secret
        self.host.tools.providers["calculate"] = self.business
        definition = AgentDefinition.model_validate((await self.host.agents.get("worker"))["definition"])
        definition.model.credential_ref = "env:OEA_DISCOVERY_TEST_KEY"
        await self.host.agents.save(definition)
        requests = []
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                requests.append(options["tools"])
                return call("load", "tool_guide", {"name": "calculate"})
        self.host.model_api = ModelFixture()
        with patch.dict("os.environ", {"OEA_DISCOVERY_TEST_KEY": secret}):
            run = await self.submit()
            with self.assertRaises(RuntimeFault) as rejected:
                await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(rejected.exception.code, "TOOL_SCHEMA_CONTAINS_SECRET")
        self.assertEqual(len(requests), 1)
        self.assertNotIn(secret, json.dumps(requests))
        self.assertEqual(self.business.calls, [])
