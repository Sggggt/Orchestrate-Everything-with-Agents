"""Project deletion respects live runtime ownership and independent projects."""

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, MCPServer
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import encode
from oea.tools.contracts import ToolSpec


class IdleConnection:
    def __init__(self, definition):
        self.definition, self.closed = definition, False
    async def aclose(self):
        self.closed = True


class ProjectRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-project-runtime-")
        self.host = await Host(Path(self.temporary.name) / "state").__aenter__()
        await self.host.projects.create("Primary", project_id="p")
        await self.host.projects.create("Other", project_id="q")
    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_controller_preparation_blocks_only_its_own_project(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"state": "completed"}
        with patch.object(self.host.mcp, "_call_current", pending):
            task = asyncio.create_task(self.host.mcp.call("example", "echo", {}, "one", project_id="p", consumer_id="controller"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                with self.assertRaises(RuntimeFault) as busy:
                    await self.host.projects.delete("p", "Primary", "delete-p")
                self.assertEqual(busy.exception.code, "PROJECT_BUSY")
                deleted = await self.host.projects.delete("q", "Other", "delete-q")
                self.assertTrue(deleted["deleted"])
            finally:
                release.set()
                await task
        self.assertEqual(self.host.mcp.calling, {})
        self.assertTrue((await self.host.projects.delete("p", "Primary", "delete-p"))["deleted"])

    async def test_deleted_project_retires_only_owned_providers_and_connections(self):
        await self.host.agents.save(AgentDefinition(id="local", name="Local", project_id="p"))
        await self.host.agents.save(AgentDefinition(id="shared", name="Shared"))
        session = await self.host.sessions.create("p")
        instance = await self.host.instances.create(session["id"], "local")
        local = MCPServer(id="local-tools", project_id="p", command=(sys.executable,))
        shared = MCPServer(id="shared-tools", command=(sys.executable,))
        await self.host.mcp.save(local)
        await self.host.mcp.save(shared)
        spec = ToolSpec(name="local_alias", input_schema={"type": "object"}, output_schema={"type": "object"})
        self.host.mcp.register(local.id, 1, [{"upstream_name": "echo", "spec": spec.model_dump(mode="json")}])
        connections = {"local": IdleConnection(local), "controller": IdleConnection(shared), "instance": IdleConnection(shared),
                       "other": IdleConnection(shared), "shared": IdleConnection(shared)}
        keys = {"local": (local.id, 1, "shared"), "controller": (shared.id, 1, self.host.mcp.controller_scope("p", "controller")),
                "instance": (shared.id, 1, encode(["instance", instance["id"]])),
                "other": (shared.id, 1, self.host.mcp.controller_scope("q", "controller")), "shared": (shared.id, 1, "shared")}
        self.host.mcp.connections.update({keys[name]: connection for name, connection in connections.items()})
        result = await self.host.projects.delete("p", "Primary", "delete")
        self.assertTrue(result["deleted"])
        self.assertNotIn("local", self.host.definitions)
        self.assertIn("shared", self.host.definitions)
        self.assertNotIn(local.id, self.host.mcp.configurations)
        for name in ("local", "controller", "instance"):
            self.assertTrue(connections[name].closed)
            self.assertNotIn(keys[name], self.host.mcp.connections)
        for name in ("other", "shared"):
            self.assertFalse(connections[name].closed)
            self.assertIn(keys[name], self.host.mcp.connections)
        for cache in (self.host.tools.providers, self.host.tools.validators, self.host.tools.contracts):
            self.assertNotIn("local_alias", cache)

    async def test_project_deletion_fact_can_finish_a_live_event_stream(self):
        stream = self.host.events_service.stream("p")
        first = await anext(stream)
        self.assertEqual(first["kind"], "project.created")
        pending = asyncio.create_task(anext(stream))
        await self.host.projects.delete("p", "Primary", "delete")
        last = await asyncio.wait_for(pending, 5)
        self.assertEqual(last["kind"], "project.deleted")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        page = await self.host.events_service.list("p")
        self.assertTrue(page["project_deleted"])
        self.assertEqual([event["kind"] for event in page["items"]], ["project.deleted"])
        await self.host.projects.create("Recreated", project_id="p")
        fresh = self.host.events_service.stream("p")
        self.assertEqual((await anext(fresh))["kind"], "project.deleted")
        self.assertEqual((await anext(fresh))["kind"], "project.created")
        await fresh.aclose()
