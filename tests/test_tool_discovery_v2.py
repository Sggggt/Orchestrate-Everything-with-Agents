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

    async def save(self, *, loading="deferred", functions=("calculate",)):
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="fixture"), tools=Tools(loading=loading, functions=functions)))

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
