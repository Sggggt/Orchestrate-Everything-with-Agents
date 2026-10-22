"""Explicit response views and the public SDK's optional-dependency boundary."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx
from pydantic import BaseModel, ConfigDict

from oea import AgentConfig, PlatformClient, Runtime, RuntimeFault, WorkflowBuilder
from ma_runtime.bridges.dispatch import dispatch
from ma_runtime.models import json_dump
from ma_runtime.web.app import create_app


class LegacyResourceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resources: list[dict]


class ProtocolViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-response-views-")
        self.rt = await Runtime.open(self.temp.name).__aenter__()

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def test_rpc_header_selects_current_and_legacy_resource_shapes(self):
        await self.rt.resources.configure({"key": "fixture", "limit": 2})
        app = create_app(runtime=self.rt)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app)) as client:
            value = await client.resources.inspect()
            self.assertIn("capacities", value)
            client.http.headers.pop("X-OEA-View-Version")
            LegacyResourceView.model_validate(await client.resources.inspect())
            client.http.headers["X-OEA-View-Version"] = "9.0"
            with self.assertRaises(RuntimeFault) as invalid:
                await client.resources.inspect()
            self.assertEqual(invalid.exception.code, "UNSUPPORTED_VIEW_VERSION")

    async def test_legacy_mutation_is_rejected_before_committing_new_graph_semantics(self):
        graph = (WorkflowBuilder().node("start", "start", failure_policy="collect_independent")
                 .node("end", "end", outputs={"ok": True}).connect("start", "end").build())
        params = {"project_id": "default", "name": "Selected policies", "idempotency_key": "one", "graph": graph}
        with self.assertRaises(RuntimeFault) as denied:
            await dispatch(self.rt, "workflows.create", params, view_version="1.0")
        self.assertEqual(denied.exception.code, "VIEW_VERSION_REQUIRED")
        self.assertFalse((await self.rt.workflows.list("default")).items)
        saved = await dispatch(self.rt, "workflows.create", params, view_version="1.1")
        app = create_app(runtime=self.rt)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app), view_version="1.0") as client:
            with self.assertRaises(RuntimeFault) as read:
                await client.workflows.get(saved.id)
            self.assertEqual(read.exception.code, "VIEW_VERSION_REQUIRED")
            with self.assertRaises(RuntimeFault):
                await client.workflows.update(saved.id, saved.revision, name="Must not commit")
            self.assertEqual((await self.rt.workflows.get(saved.id)).name, "Selected policies")
            response = await client.http.get("/api/workflows/" + saved.id)
            self.assertEqual(response.json()["error"]["code"], "VIEW_VERSION_REQUIRED")
            client.http.headers["X-OEA-View-Version"] = "1.1"
            response = await client.http.get("/api/workflows/" + saved.id)
            self.assertEqual(response.json()["graph"]["nodes"][0]["config"]["failure_policy"], "collect_independent")

    async def test_actual_legacy_models_read_default_snapshots(self):
        path = os.environ.get("OEA_LEGACY_MODELS")
        if not path:
            self.skipTest("Supply a frozen prior release models.py for installed-version compatibility validation")
        name = "ma_runtime._legacy_contract_models"
        spec = importlib.util.spec_from_file_location(name, Path(path))
        legacy = importlib.util.module_from_spec(spec)
        sys.modules[name] = legacy
        self.addCleanup(sys.modules.pop, name)
        spec.loader.exec_module(legacy)
        config = await dispatch(self.rt, "agents.configure", {"config": {"id": "compatible", "name": "Compatible", "kind": "mock"}}, view_version="1.0")
        legacy.AgentConfig.model_validate({k: v for k, v in config.items() if k not in {"has_api_key", "descriptor", "active_runs", "model_setup"}})
        graph = WorkflowBuilder().node("start", "start").node("end", "end", outputs={"ok": True}).connect("start", "end").build()
        value = await dispatch(self.rt, "workflows.create", {"project_id": "default", "name": "Compatible", "idempotency_key": "legacy", "graph": graph}, view_version="1.0")
        legacy.WorkflowInfo.model_validate_json(json_dump(value))
        run = await self.rt.workflows.run(value.id, {}, "run")
        await self.rt.workflows.result(run.id, timeout=3)
        legacy.WorkflowRunSnapshot.model_validate_json(json_dump(await dispatch(self.rt, "workflows.get_run", {"run_id": run.id}, view_version="1.0")))
        session = await self.rt.sessions.get(run.session_id)
        legacy.SessionInfo.model_validate_json(json_dump(session))
        thread = await self.rt.threads.create(session.id, "compatible")
        legacy.ThreadSnapshot.model_validate_json(json_dump(await dispatch(self.rt, "threads.get", {"thread_id": thread.id}, view_version="1.0")))
        task = await self.rt.tasks.submit({"session_id": session.id, "thread_id": thread.id, "agent_id": "compatible", "idempotency_key": "legacy-task", "payload": {"input": "Compatible task"}})
        await self.rt.tasks.result(task.id, timeout=5)
        legacy.TaskSnapshot.model_validate_json(json_dump(await dispatch(self.rt, "tasks.get", {"task_id": task.id}, view_version="1.0")))
        message = await self.rt.messages.send({"session_id": session.id, "target_thread_id": thread.id, "idempotency_key": "legacy-message", "payload": {"text": "Compatible notification"}})
        await self.rt.messages.reject(message.id, "No execution requested", "reject")
        legacy.MessageSnapshot.model_validate_json(json_dump(await dispatch(self.rt, "messages.get", {"message_id": message.id}, view_version="1.0")))
        legacy.SessionSnapshot.model_validate_json(json_dump(await self.rt.sessions.snapshot(session.id)))
        for node in await dispatch(self.rt, "workflows.node_types", {}, view_version="1.0"):
            legacy.WORKFLOW_CONFIGS[node["type"]].model_validate(node["defaults"])

    async def test_legacy_clients_cannot_drop_explicit_model_reasoning_settings(self):
        config = AgentConfig(id="configured", name="Configured", kind="mock", reasoning_effort="none")
        with self.assertRaises(RuntimeFault) as rejected:
            await dispatch(self.rt, "agents.configure", {"config": config}, view_version="1.0")
        self.assertEqual(rejected.exception.code, "VIEW_VERSION_REQUIRED")
        self.assertNotIn(config.id, self.rt.agent_configs)
        await dispatch(self.rt, "agents.configure", {"config": config}, view_version="1.1")
        app = create_app(runtime=self.rt)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app), view_version="1.0") as client:
            for method, params in (("agents.list", {}), ("agents.describe", {"agent_id": config.id}),
                                   ("agents.configure", {"config": {"id": config.id, "name": "Old client", "kind": "mock"}})):
                with self.assertRaises(RuntimeFault) as denied:
                    await client.call(method, params)
                self.assertEqual(denied.exception.code, "VIEW_VERSION_REQUIRED")
            for url in ("/api/agents", "/api/bootstrap"):
                response = await client.http.get(url)
                self.assertEqual(response.json()["error"]["code"], "VIEW_VERSION_REQUIRED")
            response = await client.http.put("/api/agents/" + config.id, json={"id": config.id, "name": "Old client", "kind": "mock"})
            self.assertEqual(response.json()["error"]["code"], "VIEW_VERSION_REQUIRED")
            self.assertEqual(self.rt.agent_configs[config.id].reasoning_effort, "none")
            client.http.headers["X-OEA-View-Version"] = "1.1"
            response = await client.http.put("/api/agents/" + config.id, json={**config.model_dump(), "reasoning_effort": None})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("reasoning_effort", response.json())

    async def test_public_modules_import_without_optional_server_dependencies(self):
        source = '''
import importlib.abc, sys
class RejectOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi','uvicorn','mcp','langgraph','PIL'}:
            raise ImportError('optional dependency requested: ' + fullname)
sys.meta_path.insert(0, RejectOptional())
from oea.client import PlatformClient
from oea.execution import AgentAdapter, ExecutionRequest
from oea.context import RunContext
from oea.tools import ToolSpec, ToolProvider
assert 'ma_runtime.runtime' not in sys.modules
assert 'apsw' not in sys.modules
from oea.workflows import WorkflowBuilder
from ma_runtime.tools import ToolSpec as LegacyToolSpec
assert LegacyToolSpec is ToolSpec
import oea, ma_runtime
assert oea.PlatformClient is ma_runtime.PlatformClient is PlatformClient
assert oea.ExecutionRequest is ExecutionRequest
assert oea.WorkflowBuilder is WorkflowBuilder
'''
        result = subprocess.run([sys.executable, "-B", "-c", source], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
