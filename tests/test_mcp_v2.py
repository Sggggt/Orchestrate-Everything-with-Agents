"""Actual MCP stdio -> HTTP -> shared service integration, without a model call."""

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import uvicorn
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from oea.runtime.host import Host
from oea.contracts.errors import RuntimeFault
from oea.web.app import create_app


def value(result):
    return result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-mcp-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state").__aenter__()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        port = self.socket.getsockname()[1]
        app = create_app(host=self.host, client_url="http://127.0.0.1:" + str(port))
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False, timeout_graceful_shutdown=2))
        self.serving = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.serving.done():
                    await self.serving
                await asyncio.sleep(0.01)
        self.errlog = (self.root / "mcp-stderr.log").open("w", encoding="utf-8")
        environment = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                       "TEMP": os.environ["TEMP"], "TMP": os.environ["TMP"]}
        self.parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "oea", "mcp", "--data-dir", str(self.host.root)], env=environment)

    @asynccontextmanager
    async def connected(self):
        # AnyIO cancel scopes must enter and exit in the same test task.
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(self.parameters, errlog=self.errlog))
            self.client = await stack.enter_async_context(ClientSession(read, write, client_info=types.Implementation(name="example-agent", version="1")))
            await self.client.initialize()
            yield

    async def asyncTearDown(self):
        self.errlog.close()
        self.server.should_exit = True
        await asyncio.wait_for(self.serving, 10)
        await self.host.aclose()
        self.temporary.cleanup()

    async def call(self, operation, params, consumer="conversation-a"):
        return await self.client.call_tool("platform_call", {"consumer_id": consumer, "operation": operation, "params": params})

    async def test_exactly_two_tools_and_guide_uses_the_actual_operation_schema(self):
        async with self.connected():
            await self.check_discovery()

    async def test_default_mcp_home_connects_to_existing_host_without_a_directory_argument(self):
        # The isolated home supplies the same metadata as the explicitly selected host.
        home = self.root / "standard-home"
        data = home / "data"
        data.mkdir(parents=True)
        for name in ("client.json", "client-token"):
            (data / name).write_bytes((self.host.root / name).read_bytes())
        self.parameters.args = ["-B", "-m", "oea", "mcp"]
        self.parameters.env["OEA_HOME"] = str(home)
        async with self.connected():
            result = value(await self.call("host.status", {}))
            self.assertIn("version", result)
        self.assertFalse((data / "oea.sqlite").exists())

    async def test_external_mcp_can_remove_all_three_registrations_without_starting_business_tools(self):
        from oea import AgentDefinition, MCPServer, SkillSource
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        await self.host.mcp.save(MCPServer(id="service", project_id="p", command=("not-installed",)))
        source = self.root / "skill"
        source.mkdir()
        (source / "SKILL.md").write_text("Read original evidence.", encoding="utf-8")
        await self.host.skills.register(SkillSource(id="guide", path=source, project_id="p"))
        (source / "SKILL.md").unlink()
        source.rmdir()
        async with self.connected():
            for area, key, identity in (("agents", "agent_id", "worker"), ("mcp", "server_id", "service"), ("skills", "skill_id", "guide")):
                guide = await self.client.call_tool("platform_guide", {"consumer_id": "deletion", "area": area, "action": "remove"})
                self.assertFalse(guide.isError)
                result = await self.call(area + ".remove", {key: identity, "project_id": "p", "expected_revision": 1}, consumer="deletion")
                self.assertFalse(result.isError)
                self.assertEqual(value(await self.call(area + ".list", {"project_id": "p"}, consumer="deletion"))["items"], [])
        self.assertEqual(self.host.mcp.connections, {})

    async def check_discovery(self):
        catalog = await self.client.list_tools()
        self.assertEqual({tool.name for tool in catalog.tools}, {"platform_guide", "platform_call"})
        self.assertTrue(all("ctx" not in tool.inputSchema.get("properties", {}) for tool in catalog.tools))
        result = await self.client.call_tool("platform_guide", {"consumer_id": "conversation-a", "area": "runs", "action": "submit"})
        self.assertFalse(result.isError)
        guide = value(result)
        if "result_ref" in guide:
            selected = await self.call("results.read", {"result_ref": guide["result_ref"], "project_id": guide["project_id"], "json_pointer": "/name"})
            self.assertEqual(value(selected)["value"], "runs.submit")
        else:
            self.assertEqual(guide["name"], "runs.submit")
            self.assertEqual(guide["parameters_schema"], self.host.registry.describe("runs.submit")["parameters_schema"])
        unavailable = await self.call("resources.force_release", {})
        self.assertTrue(unavailable.isError)
        self.assertIn("METHOD_NOT_FOUND", "".join(item.text for item in unavailable.content if item.type == "text"))
        text = "".join(item.text for item in unavailable.content if item.type == "text")
        transported = json.loads(text[text.index('{"error"'):])["error"]
        with self.assertRaises(RuntimeFault) as local_error:
            await self.host.registry.call("resources.force_release", {})
        self.assertEqual(transported["diagnostic"], local_error.exception.as_dict()["diagnostic"])

    async def test_message_source_uses_the_connected_client_name(self):
        from oea import AgentDefinition
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        session = await self.host.sessions.create("p")
        instance = await self.host.instances.create(session["id"], "worker", name="one")
        async with self.connected():
            message = value(await self.call("messages.send", {"spec": {"session_id": session["id"], "recipients": [instance["id"]], "content": "Information", "idempotency_key": "message"}}))
        self.assertEqual(message["source"]["kind"], "leader")
        self.assertEqual(message["source"]["name"], "example-agent")

    async def test_large_write_reply_is_read_by_reference_without_repeating_the_write(self):
        async with self.connected():
            await self.check_references()

    async def check_references(self):
        created = await self.call("projects.create", {"name": "Project", "project_id": "p"})
        self.assertFalse(created.isError)
        definition = {"id": "review", "name": "Review", "project_id": "p", "instructions": {"agent_md": "Bounded public guidance. " * 1800}}
        saved = await self.call("agents.save", {"definition": definition, "expected_revision": 0})
        self.assertFalse(saved.isError)
        reference = value(saved)
        self.assertIn("result_ref", reference)
        read = await self.call("results.read", {"result_ref": reference["result_ref"], "project_id": "p", "json_pointer": "/definition/id"})
        self.assertFalse(read.isError)
        self.assertEqual(value(read)["value"], "review")
        revision = await self.host.agents.get("review")
        self.assertEqual(revision["revision"], 1)
        denied = await self.call("results.read", {"result_ref": reference["result_ref"], "project_id": "p", "json_pointer": "/definition/id"}, consumer="conversation-b")
        self.assertTrue(denied.isError)
        denied = await self.call("results.read", {"result_ref": reference["result_ref"], "project_id": "other", "json_pointer": "/definition/id"})
        self.assertTrue(denied.isError)
        text = await self.call("results.read", {"result_ref": reference["result_ref"], "project_id": "p", "json_pointer": "/definition/instructions/agent_md", "limit": 128})
        self.assertIsNotNone(value(text)["next_offset"])
        changed = revision["definition"]
        changed["description"] = "A new configuration revision"
        await self.host.agents.save(changed, expected_revision=1)
        stale = await self.call("results.read", {"result_ref": reference["result_ref"], "project_id": "p", "json_pointer": "/definition/id"})
        self.assertTrue(stale.isError)
        self.assertIn("SOURCE_VERSION_CHANGED", "".join(item.text for item in stale.content if item.type == "text"))
        self.assertEqual((await self.host.agents.get("review"))["revision"], 2)
