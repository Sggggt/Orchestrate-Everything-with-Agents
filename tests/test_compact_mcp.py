"""Scoped MCP contracts; isolated local state, with no business-service calls."""

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

from ma_runtime import AgentConfig, AgentSkill, PlatformClient, Runtime
from ma_runtime.bridges.compact import AREAS, CompactBridge
from ma_runtime.bridges.dispatch import dispatch, methods
from ma_runtime.bridges.gateway import gateway_request
from ma_runtime.errors import RuntimeFault
from ma_runtime.models import json_dump
from ma_runtime.web.app import create_app


class CompactMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ma-compact-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root).__aenter__()

        async def invoke(method, params):
            return json.loads(json_dump(await dispatch(self.rt, method, params)))

        self.bridge = CompactBridge(invoke)

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def test_action_catalog_covers_rpc_without_loading_all_schemas(self):
        exposed = {
            method for _, actions in AREAS.values() for method in actions.values()
        }
        self.assertLessEqual(set(methods(self.rt)), exposed)
        overview = self.bridge.guide()
        self.assertNotIn("schemas", overview)
        selected = self.bridge.guide("profiles", "skill_put")
        self.assertEqual(set(selected["schemas"]), {"AgentSkill"})
        self.assertNotIn("MCPServerConfig", json_dump(selected))
        self.assertEqual(
            self.bridge.guide("tasks", "result")["parameters"][1]["default"], 0
        )

    async def test_call_rejects_undocumented_methods_and_run_impersonation(self):
        for area, action, params in [
            ("shell", "exec", {"command": "echo nope"}),
            ("tasks", "submit", {"spec": {}, "caller_run_id": "fabricated"}),
            ("agents", "describe", {}),
            ("tasks", "result", {"task_id": "anything", "timeout": None}),
        ]:
            with self.assertRaises(RuntimeFault):
                await self.bridge.call(area, action, params)

    async def test_partial_configuration_preserves_instructions_and_skills(self):
        await self.rt.agents.configure(
            AgentConfig(
                id="writer",
                name="Writer",
                kind="mock",
                agent_md="# Keep these rules",
                skills=(
                    AgentSkill(
                        id="style",
                        name="Style",
                        description="Writing style",
                        content="Be specific",
                    ),
                ),
            )
        )
        reply = await self.bridge.call(
            "agents", "patch", {"agent_id": "writer", "changes": {"name": "Updated"}}
        )
        self.assertEqual(reply["result"]["name"], "Updated")
        current = await self.rt.agents.describe("writer")
        self.assertEqual(current["agent_md"], "# Keep these rules")
        self.assertEqual(current["skills"][0]["id"], "style")
        with self.assertRaises(RuntimeFault):
            await self.bridge.call(
                "agents", "patch", {"agent_id": "writer", "changes": {"id": "other"}}
            )

    async def test_large_output_is_read_by_pointer_without_repeating_action(self):
        count = 0

        async def invoke(method, params):
            nonlocal count
            count += 1
            return {"big": "日志" * 6000, "a/b": {"~result": ["answer"]}}

        bridge = CompactBridge(invoke)
        response = await bridge.call("sessions", "create", {"title": "One write"})
        self.assertLess(len(json_dump(response)), 3500)
        selected = await bridge.call(
            "control",
            "read_result",
            {
                "result_ref": response["result_ref"],
                "pointer": "/a~1b/~0result/0",
            },
        )
        self.assertEqual(json.loads(selected["result"]["text"]), "answer")
        self.assertEqual(count, 1)
        with patch("ma_runtime.bridges.compact.time.monotonic", return_value=10**20):
            with self.assertRaisesRegex(RuntimeFault, "Do not repeat a mutation"):
                bridge.cache.read(response["result_ref"])
        self.assertEqual(count, 1)

    async def test_tool_directory_defers_individual_schema(self):
        async def invoke(method, params):
            return {
                "server_id": "fixture",
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo",
                        "inputSchema": {"type": "object"},
                    },
                    {
                        "name": "other",
                        "description": "Other",
                        "inputSchema": {"type": "object"},
                    },
                ],
            }

        bridge = CompactBridge(invoke)
        index = await bridge.call("mcp", "tools", {"server_id": "fixture"})
        self.assertNotIn("inputSchema", json_dump(index))
        selected = await bridge.call(
            "mcp", "tools", {"server_id": "fixture", "tool_name": "echo"}
        )
        self.assertEqual(len(selected["result"]["tools"]), 1)
        self.assertIn("inputSchema", selected["result"]["tools"][0])

    async def test_gateway_config_skills_and_file_transfer(self):
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):

            def client_factory(*args, **kwargs):
                return PlatformClient(
                    "http://127.0.0.1",
                    app.state.token,
                    transport=httpx.ASGITransport(app=app),
                )

            with patch(
                "ma_runtime.bridges.gateway.PlatformClient.from_data_dir",
                side_effect=client_factory,
            ):
                status = await gateway_request(self.root, "@status", {})
                self.assertEqual(status["agent_count"], 0)
                config = await gateway_request(
                    self.root, "@configure", {"changes": {"reserved_external_slots": 0}}
                )
                self.assertEqual(config["reserved_external_slots"], 0)
                self.assertEqual(config["max_concurrency"], 4)
                parsed = await gateway_request(
                    self.root,
                    "@skill_parse",
                    {
                        "skill_id": "check",
                        "markdown": "---\nname: Check\ndescription: Use for checks\n---\n# Check",
                    },
                )
                self.assertEqual(parsed["name"], "Check")
                session = await self.rt.sessions.create(title="Files")
                source = self.root / "输入 file.txt"
                source.write_text("round trip", encoding="utf-8")
                artifact = await gateway_request(
                    self.root,
                    "@upload_file",
                    {"session_id": session.id, "path": str(source)},
                )
                output = self.root / "download.txt"
                await gateway_request(
                    self.root,
                    "@download_file",
                    {"artifact_id": artifact["id"], "path": str(output)},
                )
                self.assertEqual(output.read_text(encoding="utf-8"), "round trip")
                with self.assertRaisesRegex(RuntimeFault, "overwrite"):
                    await gateway_request(
                        self.root,
                        "@download_file",
                        {"artifact_id": artifact["id"], "path": str(output)},
                    )
                self.assertEqual(output.read_text(encoding="utf-8"), "round trip")

    async def test_real_stdio_compact_bridge_reuses_http_host(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app = create_app(runtime=self.rt, client_url=f"http://127.0.0.1:{port}")
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False)
        )
        hosting = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if hosting.done():
                        await hosting
                    await asyncio.sleep(0.01)
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-B",
                    "-m",
                    "ma_runtime.cli",
                    "mcp",
                    "--connect",
                    "--compact",
                    "--data-dir",
                    str(self.root),
                ],
                env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            with open(os.devnull, "w") as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        catalog = (await client.list_tools()).tools
                        self.assertEqual(
                            {t.name for t in catalog},
                            {"platform_guide", "platform_call"},
                        )
                        self.assertLess(
                            len(json_dump([t.model_dump() for t in catalog])), 2500
                        )
                        resources = (await client.list_resources()).resources
                        self.assertTrue({"multi-agent://tool-index", "multi-agent://projects",
                                         "multi-agent://output-contract"} <= {str(r.uri) for r in resources})
                        index = await client.read_resource("multi-agent://tool-index")
                        tool_index = json.loads(index.contents[0].text)
                        self.assertIn("projects", tool_index["areas"])
                        self.assertIn("workflows", tool_index["areas"])
                        projects = await client.read_resource("multi-agent://projects")
                        self.assertEqual(json.loads(projects.contents[0].text)["result"][0]["id"], "default")

                        async def call(area, action, arguments):
                            reply = await client.call_tool(
                                "platform_call",
                                {"area": area, "action": action, "params": arguments},
                            )
                            self.assertFalse(reply.isError)
                            value = reply.structuredContent or json.loads(
                                reply.content[0].text
                            )
                            self.assertTrue(value["ok"], value)
                            return value["result"]

                        self.assertEqual(
                            (await call("control", "status", {}))["agent_count"], 0
                        )
                        await call(
                            "agents",
                            "configure",
                            {
                                "config": {
                                    "id": "fixture",
                                    "name": "Fixture",
                                    "kind": "mock",
                                }
                            },
                        )
                        session = await call(
                            "sessions", "create", {"title": "Compact MCP"}
                        )
                        thread = await call(
                            "threads",
                            "create",
                            {"session_id": session["id"], "agent_id": "fixture"},
                        )
                        task = await call(
                            "threads",
                            "turn",
                            {
                                "thread_id": thread["id"],
                                "content": "hello",
                                "idempotency_key": "one-turn",
                            },
                        )
                        result = await call(
                            "tasks", "result", {"task_id": task["id"], "timeout": 5}
                        )
                        self.assertIn("output", result)
                        self.assertEqual(len(await self.rt.agents.list()), 1)
                        self.assertEqual(
                            (await self.rt.tasks.get(task["id"])).state, "SUCCEEDED"
                        )
                        workflow = await call("workflows", "create", {
                            "project_id": "default", "name": "MCP 工作流", "idempotency_key": "stdio-workflow",
                        })
                        workflow_run = await call("workflows", "run", {
                            "workflow_id": workflow["id"], "inputs": {"via": "stdio-mcp"},
                            "expected_revision": workflow["revision"], "idempotency_key": "stdio-run",
                        })
                        workflow_result = await call("workflows", "result", {"run_id": workflow_run["id"], "timeout": 5})
                        self.assertEqual(workflow_result["outputs"], {"result": {"via": "stdio-mcp"}})
                        run_snapshot = await call("workflows", "get_run", {"run_id": workflow_run["id"]})
                        self.assertEqual({node["state"] for node in run_snapshot["nodes"]}, {"SUCCEEDED"})
                        self.assertTrue((await call("workflows", "delete", {"workflow_id": workflow["id"], "confirmed": True}))["deleted"])
                        self.assertEqual((await call("workflows", "list", {"project_id": "default", "deleted_only": True}))["items"][0]["id"], workflow["id"])
                        self.assertEqual((await call("workflows", "restore", {"workflow_id": workflow["id"]}))["state"], "ACTIVE")
        finally:
            server.should_exit = True
            await asyncio.wait_for(hosting, 10)
            sock.close()


if __name__ == "__main__":
    unittest.main()
