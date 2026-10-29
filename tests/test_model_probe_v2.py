"""Connection probes isolate capabilities while retaining real model receipts."""

import json
from pathlib import Path
import tempfile
import unittest

from oea import Agent, AgentDefinition, Completion, MCPBinding, Model, SkillBinding, Tools
from oea.contracts.configuration import Execution, Instructions, Manager, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class ModelProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-model-probe-")
        self.root = Path(self.temporary.name)
        self.requests = []
        outer = self
        class ModelFixture:
            async def complete(self, model, messages, **options):
                outer.requests.append((model, messages, options))
                return Completion(text="READY", usage={"prompt_tokens": 80, "completion_tokens": 3})
        self.host = await Host(self.root / "state", model_api=ModelFixture()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.session = await self.host.sessions.create("p")
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="main-fixture", max_output_tokens=131072),
            manager=Manager(model=Model(base_url="https://model.invalid/v1", model="manager-fixture")),
            instructions=Instructions(system="BUSINESS_INSTRUCTIONS " * 100),
            tools=Tools(functions=("unavailable_business",), mcp=(MCPBinding(server_id="unavailable"),)),
            skills=(SkillBinding(id="business", path=self.root / "unopened-skill", required=True),),
            workspace=Workspace(project=self.root / "unopened-project"), execution=Execution(program=("unavailable-program",)))
        await self.host.agents.save(self.definition)

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_probe_is_one_small_call_without_business_setup_and_replays_receipt(self):
        before = await self.host.agents.get("worker")
        handle = await self.host.agents.probe("worker", self.session["id"], "connection")
        result = await self.host.runs.result(handle.id, timeout=5)
        self.assertEqual(result.text, "READY")
        self.assertEqual(len(self.requests), 1)
        model, messages, options = self.requests[0]
        self.assertEqual(model.model, "main-fixture")
        self.assertEqual(options["output_tokens"], 128)
        self.assertEqual(options["tools"], [])
        self.assertNotIn("BUSINESS_INSTRUCTIONS", json.dumps(messages))
        self.assertNotIn("unopened", json.dumps(messages))
        self.assertLess(len(json.dumps(messages)), 2048)
        again = await self.host.agents.probe("worker", self.session["id"], "connection")
        self.assertEqual(again.id, handle.id)
        self.assertTrue(again.reused)
        await self.host.runs.result(again.id, timeout=5)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(await self.host.agents.get("worker"), before)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM skill_loads")["n"]), 0)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM run_mcp_bindings")["n"]), 0)
        budget = (await self.host.runs.inspect(handle.id))["budget"]
        self.assertEqual(budget["provider_reported"]["input_tokens"], 80)
        self.assertEqual(budget["usage"]["model_calls"], 1)

    async def test_manager_probe_uses_only_its_model_and_requires_collaboration(self):
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.agents.probe("worker", self.session["id"], "manager", role="manager")
        self.assertEqual(denied.exception.code, "MANAGER_UNAVAILABLE")
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        handle = await self.host.agents.probe("worker", self.session["id"], "manager", role="manager")
        await self.host.runs.result(handle.id, timeout=5)
        self.assertEqual(self.requests[0][0].model, "manager-fixture")
        self.assertEqual(self.requests[0][2]["tools"], [])

    async def test_foreign_session_rejects_probe_before_creating_any_work(self):
        await self.host.projects.create("Other", project_id="q")
        session = await self.host.sessions.create("q")
        with self.assertRaises(RuntimeFault):
            await self.host.agents.probe("worker", session["id"], "foreign")
        self.assertEqual(self.requests, [])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM runs")["n"]), 0)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM instances")["n"]), 0)

    async def test_saved_sdk_agent_uses_the_shared_probe_operation(self):
        async with Agent.from_saved("worker", host=self.host, session_id=self.session["id"]) as agent:
            self.assertEqual((await agent.aprobe(idempotency_key="sdk-probe")).text, "READY")
        self.assertTrue(self.host.opened)
        self.assertEqual(len(self.requests), 1)
