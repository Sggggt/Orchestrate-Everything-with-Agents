"""Read-only decision facts, shared submission boundaries and qualified observations."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ma_runtime import AgentConfig, Runtime, RuntimeFault
from ma_runtime.bridges.compact import CompactBridge
from ma_runtime.bridges.dispatch import dispatch
from ma_runtime.models import TaskSpec, json_dump
from ma_runtime.store import one
import test_model_configuration as model_fixture


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-admission-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root).__aenter__()
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock"))
        self.session = await self.rt.sessions.create()
        self.thread = await self.rt.threads.create(self.session.id, "worker")
        self.spec = TaskSpec(session_id=self.session.id, thread_id=self.thread.id, agent_id="worker", idempotency_key="work", payload={"input": "inspect"})

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def snapshot(self):
        return await self.rt.store.call(lambda db: (db.total_changes(), tuple(one(db, "SELECT COUNT(*) AS n FROM " + table).n
            for table in ("tasks", "threads", "events", "execution_budgets", "resource_leases", "model_observations"))))

    async def test_readonly_preflight_and_live_capacity_without_defining_resources(self):
        before = await self.snapshot()
        capacities = copy.deepcopy(self.rt.resources.capacities)
        with patch.object(self.rt.mcp, "connect", AsyncMock(side_effect=AssertionError("must not connect"))):
            result = await self.rt.admission.check(self.spec)
            self.assertTrue(result["admissible"])
            self.assertTrue(result["ready_now"])
            self.assertFalse(result["reservation"])
            summary = await self.rt.admission.describe("worker")
            self.assertEqual(summary["observations"]["sample_count"], 0)
            self.assertEqual(summary["purpose"]["source"], "configured_agent_md")
        self.assertEqual(before, await self.snapshot())
        self.assertEqual(capacities, self.rt.resources.capacities)
        self.rt.resources.quantities["execution:agent:worker"] = {"held": 4}
        waiting = await self.rt.admission.check(self.spec)
        self.assertTrue(waiting["admissible"])
        self.assertFalse(waiting["ready_now"])
        self.assertEqual(waiting["queue_reasons"][0]["reason"], "CAPACITY")
        self.rt.resources.quantities.clear()

    async def test_shared_scope_policy_and_receipt_rules(self):
        project = await self.rt.projects.create("Second", str(self.root), "project")
        await self.rt.agents.configure(AgentConfig(id="other", name="Other", project_id=project.id, kind="mock"))
        for changes, code in (({"agent_id": "other"}, "PROJECT_MISMATCH"),
                              ({"tool_policy": {"required": ["read_file"]}}, "PERMISSION_DENIED"),
                              ({"skill_policy": {"required": ["missing"]}}, "PERMISSION_DENIED")):
            spec = TaskSpec.model_validate({**self.spec.model_dump(), **changes})
            result = await self.rt.admission.check(spec)
            self.assertEqual(result["blockers"][0]["code"], code)
            with self.assertRaises(RuntimeFault) as error:
                await self.rt.tasks.submit(spec)
            self.assertEqual(error.exception.code, code)
        task = await self.rt.tasks.submit(self.spec)
        await self.rt.tasks.result(task.id, timeout=5)
        replay = await self.rt.admission.check(self.spec)
        self.assertEqual(replay["existing_task"]["id"], task.id)
        conflict = await self.rt.admission.check(self.spec.model_copy(update={"payload": {"input": "different"}}))
        self.assertEqual(conflict["blockers"][0]["code"], "IDEMPOTENCY_CONFLICT")
        invalid = await self.rt.admission.check({"unknown": True})
        self.assertEqual(invalid["checks"]["task_schema"], "invalid")

    async def test_candidates_page_and_missing_cached_dependencies(self):
        await self.rt.mcp.configure({"id": "service", "name": "Service", "command": sys.executable})
        await self.rt.agents.configure(AgentConfig(id="bound", name="Bound", kind="mock", mcp_bindings=({"server_id": "service", "tools": ["lookup"]},)))
        before = await self.snapshot()
        page = await self.rt.admission.candidates("default", required_tools=["mcp:service:lookup"], limit=1)
        self.assertEqual(page["items"][0]["agent_id"], "bound")
        self.assertTrue(page["items"][0]["eligible"])
        self.assertEqual(page["items"][0]["unknown"][0]["code"], "MCP_CATALOG_UNKNOWN")
        second = await self.rt.admission.candidates("default", required_tools=["mcp:service:lookup"], cursor=page["next_cursor"], limit=1)
        self.assertFalse(second["items"][0]["eligible"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(before, await self.snapshot())
        self.rt.mcp.catalogs["service"] = {"discovered_at": 1, "tools": []}
        missing = await self.rt.admission.candidates("default", required_tools=["mcp:service:lookup"])
        self.assertEqual(missing["items"][0]["blockers"][0]["code"], "MCP_TOOL_UNAVAILABLE")
        model = await self.rt.admission.candidates("default", require_model=True)
        self.assertFalse(any(item["eligible"] for item in model["items"]))

    async def test_budget_scope_and_exhaustion_without_reservation(self):
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="llm", base_url="https://example.invalid", model="configured"))
        budget = await self.rt.budgets.create(self.session.id, {"max_model_calls": 1}, "budget")
        await self.rt.store.call(lambda db: db.execute("UPDATE execution_budgets SET model_calls=1 WHERE id=?", (budget["id"],)), transaction=True)
        before = await self.snapshot()
        result = await self.rt.admission.check(self.spec, budget_id=budget["id"])
        self.assertEqual(result["blockers"][0]["code"], "BUDGET_EXHAUSTED")
        self.assertEqual(before, await self.snapshot())
        invalid = await self.rt.admission.check(self.spec, budget_id="missing")
        self.assertEqual(invalid["blockers"][0]["code"], "BUDGET_SCOPE_MISMATCH")

    async def test_required_mcp_initialization_obeys_task_policy_and_cached_catalog(self):
        await self.rt.mcp.configure({"id": "service", "name": "Service", "command": sys.executable})
        self.rt.mcp.catalogs["service"] = {"discovered_at": 1, "tools": [{"name": "lookup"}, {"name": "select"}]}
        await self.rt.mcp.configure_policy("service", {"state_mode": "reinitialize", "initialization": [{"tool_name": "select", "arguments": {}, "connection_state_only": True}]})
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock", mcp_bindings=({"server_id": "service", "tools": ["lookup", "select"]},)))
        narrowed = TaskSpec.model_validate({**self.spec.model_dump(), "tool_policy": {"required": ["mcp:service:lookup"], "allowed": ["mcp:service:lookup"]}})
        denied = await self.rt.admission.check(narrowed)
        self.assertEqual(denied["blockers"][0]["code"], "MCP_INITIALIZATION_UNAVAILABLE")
        wider = TaskSpec.model_validate({**self.spec.model_dump(), "tool_policy": {"required": ["mcp:service:lookup"]}})
        self.assertTrue((await self.rt.admission.check(wider))["admissible"])
        self.rt.mcp.catalogs["service"]["tools"].pop()
        missing = await self.rt.admission.check(wider)
        self.assertEqual(missing["blockers"][0]["tool"], "mcp:service:select")

    async def test_tool_directory_preserves_recursive_schema_and_stale_fingerprint(self):
        await self.rt.mcp.configure({"id": "service", "name": "Service", "command": sys.executable})
        self.assertEqual((await self.rt.mcp.catalog("service"))["source"], "unknown")
        schema = {"$ref": "#/$defs/node", "$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}}}
        self.rt.mcp.catalogs["service"] = {"protocolVersion": "2025-11-25", "discovered_at": 1,
            "tools": [{"name": "recursive", "inputSchema": schema}, {"name": "second", "inputSchema": {"type": "object"}}]}
        page = await self.rt.mcp.catalog("service", limit=1)
        self.assertNotIn("inputSchema", json_dump(page))
        self.assertEqual(page["next_cursor"], "recursive")
        tool = await self.rt.mcp.tool("service", "recursive", page["items"][0]["fingerprint"])
        self.assertEqual(tool["tool"]["inputSchema"], schema)
        self.assertEqual(tool["validation"]["provider_tool_support"], "unknown")
        self.rt.mcp.catalogs["service"]["tools"][0]["description"] = "Changed"
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.tool("service", "recursive", page["items"][0]["fingerprint"])
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        async def invoke(method, params):
            return json.loads(json_dump(await dispatch(self.rt, method, params)))
        bridge = CompactBridge(invoke)
        self.assertEqual(set(bridge.guide("admission", "check")["schemas"]), {"TaskSpec"})
        result = await bridge.call("admission", "check", {"spec": self.spec.model_dump(mode="json")})
        self.assertTrue(result["result"]["admissible"])


class AdmissionObservationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = model_fixture.ModelConfigurationTests.asyncSetUp
    asyncTearDown = model_fixture.ModelConfigurationTests.asyncTearDown
    config = model_fixture.ModelConfigurationTests.config
    submit = model_fixture.ModelConfigurationTests.submit

    async def test_observations_are_actual_and_do_not_survive_model_configuration_change(self):
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        before = await self.rt.admission.describe("model-worker")
        self.assertEqual(before["observations"]["sample_count"], 0)
        task = await self.submit({"input": "test"})
        await self.rt.tasks.result(task.id, timeout=10)
        after = await self.rt.admission.describe("model-worker")
        self.assertEqual(after["observations"]["successful_requests"], 1)
        self.assertEqual(after["observations"]["input_tokens"], 2)
        self.assertEqual(after["observations"]["output_tokens"], 3)
        self.assertEqual(after["model"]["verification"], "observed_response")
        self.assertIsNone(after["observations"]["monetary_cost"])
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False, model="changed"))
        changed = await self.rt.admission.describe("model-worker")
        self.assertNotEqual(after["model"]["fingerprint"], changed["model"]["fingerprint"])
        self.assertEqual(changed["observations"]["sample_count"], 0)
