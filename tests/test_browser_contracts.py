"""Browser request shapes against the current HTTP/RPC service and scheduler."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from ma_runtime import AdapterEvent, AgentDescriptor, Runtime, TaskResult
from ma_runtime.web.app import create_app
from test_skill_system import package


class FeedbackReporter:
    """Emit run-authenticated feedback before the execution completes."""
    def __init__(self):
        self.sent = asyncio.Event()
        self.release = asyncio.Event()

    def describe(self):
        return AgentDescriptor(agent_id="reporter")

    async def execute(self, request, context):
        await context.send_message({"session_id": request.session_id,
            "target_thread_id": request.payload["target_thread_id"],
            "payload": {"summary_markdown": "### Progress\n\n**Discovery complete**.\n\n- Selection verified",
                        "content": "Complete peer content remains available."},
            "idempotency_key": request.task_id + ":message"})
        for index in range(request.payload.get("report_count", 1)):
            await context.report_issue(f"### Finding {index}\n\n**Needs attention**.\n\n- Verify selection",
                request.task_id + f":report:{index}", details="REPORT_BODY_FOR_PEER_ONLY")
        self.sent.set()
        await self.release.wait()
        yield AdapterEvent(type="completed", result=TaskResult(output={"text": "Feedback sent."}))

    async def aclose(self):
        self.release.set()


class BrowserContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-browser-contract-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        self.app = create_app(runtime=self.rt)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.http = httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=self.app),
            headers={"Authorization": "Bearer " + self.app.state.token})

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await self.rt.aclose()
        self.temp.cleanup()

    async def request(self, method, path, **kwargs):
        response = await self.http.request(method, "/api" + path, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def worker(self):
        return await self.request("PUT", "/agents/worker", json={"id": "worker", "name": "Worker", "kind": "mock"})

    async def test_one_shot_dependencies_use_current_rpc_and_return_actual_states(self):
        await self.worker()
        session = await self.request("POST", "/sessions", json={"title": "Browser dependencies", "project_id": "default"})
        first, second = [await self.request("POST", "/threads", json={"session_id": session["id"], "agent_id": "worker"}) for _ in range(2)]
        params = {"session_id": session["id"], "idempotency_key": "browser-dependencies", "nodes": [
            {"key": "node-0", "thread_id": first["id"], "payload": {"input": "First"}, "depends_on": []},
            {"key": "node-1", "thread_id": second["id"], "payload": {"input": "Second"}, "depends_on": ["node-0"]},
        ]}
        result = (await self.request("POST", "/rpc", json={"method": "tasks.workflow", "params": params}))["result"]
        repeated = (await self.request("POST", "/rpc", json={"method": "tasks.workflow", "params": params}))["result"]
        self.assertEqual(result["node-1"]["id"], repeated["node-1"]["id"])
        await self.rt.tasks.result(result["node-1"]["id"], timeout=10)
        task = await self.request("GET", "/tasks/" + result["node-1"]["id"])
        self.assertEqual(task["state"], "SUCCEEDED")
        self.assertEqual(task["spec"]["depends_on"], [result["node-0"]["id"]])
        self.assertEqual(task["result"]["output"], {})
        snapshot = await self.request("GET", "/sessions/" + session["id"] + "/snapshot")
        self.assertEqual(snapshot["failed_thread_ids"], [])
        self.assertEqual(snapshot["attention_thread_ids"], [])

    async def test_skill_registration_and_agent_editor_share_current_fields(self):
        directory = package(self.root)
        detail = await self.request("POST", "/skills/register-directory", json={"directory": str(directory)})
        self.assertEqual(detail["resources"], ["references/check.md"])
        file = await self.request("GET", "/skills/review/read?path=references%2Fcheck.md")
        self.assertIn("Inspect receipts", file["content"])
        await self.request("PUT", "/agents/worker", json={"id": "worker", "name": "Worker", "kind": "mock",
            "agent_md": "Follow the registered checklist.", "skill_bindings": [{"skill_id": "review"}], "mcp_bindings": []})
        bootstrap = await self.request("GET", "/bootstrap?project_id=default")
        self.assertEqual(bootstrap["skill_registry"]["items"][0]["bound_agents"], ["worker"])
        worker = next(agent for agent in bootstrap["agents"] if agent["id"] == "worker")
        self.assertEqual([binding["skill_id"] for binding in worker["skill_bindings"]], ["review"])
        docs = await self.request("GET", "/agents/worker/documents")
        self.assertEqual(docs["instructions"]["content"], "Follow the registered checklist.")
        self.assertEqual(docs["skills"][0]["id"], "review")
        await self.request("PATCH", "/skills/review", json={"enabled": False})
        self.assertFalse((await self.request("GET", "/skills/review"))["enabled"])
        await self.request("PUT", "/agents/worker", json={"id": "worker", "name": "Worker", "kind": "mock", "skill_bindings": []})
        await self.request("DELETE", "/skills/review", json={})
        self.assertTrue((directory / "SKILL.md").is_file())

    async def test_mcp_editor_discovers_calls_and_exports_current_config(self):
        fixture = Path(__file__).parent / "fixtures/mcp_server.py"
        detail = await self.request("PUT", "/mcp/servers/browser-tools", json={"id": "browser-tools", "name": "Browser tools",
            "transport": "stdio", "command": sys.executable, "args": ["-B", str(fixture)]})
        self.assertEqual(detail["id"], "browser-tools")
        detail = await self.request("POST", "/mcp/servers/browser-tools/connect", json={"consumer_id": "console"})
        self.assertEqual(detail["status"], "CONNECTED")
        self.assertGreater(detail["tool_count"], 0)
        catalog = await self.request("GET", "/mcp/servers/browser-tools/tools")
        self.assertIn("inputSchema", next(tool for tool in catalog["tools"] if tool["name"] == "echo"))
        value = await self.request("POST", "/mcp/servers/browser-tools/call", json={"tool_name": "echo",
            "arguments": {"text": "Browser transport"}, "consumer_id": "console", "idempotency_key": "browser-echo"})
        self.assertFalse(value["isError"])
        self.assertIn("Browser transport", json.dumps(value))
        self.assertEqual((await self.request("GET", "/mcp/calls?server_id=browser-tools"))[0]["tool_name"], "echo")
        config = (await self.request("GET", "/mcp/codex-config"))["mcp_servers"]["oea"]
        self.assertEqual(config["enabled_tools"], ["platform_guide", "platform_call"])
        self.assertIn("--compact", config["args"])
        await self.request("POST", "/mcp/servers/browser-tools/disconnect", json={})
        await self.request("DELETE", "/mcp/servers/browser-tools", headers={"Content-Type": "application/json"})

    async def test_agent_feedback_is_visible_before_completion_bounded_and_scoped(self):
        await self.worker()
        await self.request("PUT", "/agents/reporter", json={"id": "reporter", "name": "Reporter", "kind": "mock"})
        adapter = FeedbackReporter()
        self.rt.adapters["reporter"] = adapter
        session = await self.request("POST", "/sessions", json={"title": "Feedback", "project_id": "default"})
        target = await self.request("POST", "/threads", json={"session_id": session["id"], "agent_id": "worker"})
        source = await self.request("POST", "/threads", json={"session_id": session["id"], "agent_id": "reporter"})
        task = await self.rt.tasks.submit({"session_id": session["id"], "agent_id": "reporter", "thread_id": source["id"],
            "payload": {"target_thread_id": target["id"], "report_count": 12}, "idempotency_key": "active-feedback"})
        try:
            await asyncio.wait_for(adapter.sent.wait(), timeout=5)
            self.assertEqual((await self.rt.tasks.get(task.id)).state, "RUNNING")
            view = await self.request("GET", "/sessions/" + session["id"] + "/snapshot")
            self.assertEqual(len(view["agent_reports"]), 10)
            self.assertTrue(view["agent_reports"][0]["summary_markdown"].startswith("### Finding 11"))
            self.assertEqual(view["agent_reports"][0]["thread_id"], source["id"])
            self.assertNotIn("REPORT_BODY_FOR_PEER_ONLY", json.dumps(view))
            message = view["messages"][0]
            self.assertTrue(message["envelope"]["source_run_id"])
            self.assertIn("**Discovery complete**", message["envelope"]["payload"]["summary_markdown"])
            self.assertNotIn("Complete peer content", json.dumps(view))
            full = await self.rt.messages.get(message["id"])
            self.assertIn("Complete peer content", full.envelope.payload["content"])
            other = await self.request("POST", "/sessions", json={"title": "Other", "project_id": "default"})
            self.assertEqual((await self.request("GET", "/sessions/" + other["id"] + "/snapshot"))["agent_reports"], [])
        finally:
            adapter.release.set()
        await self.rt.tasks.result(task.id, timeout=5)
