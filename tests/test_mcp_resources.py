"""Real local MCP transports verify service-wide admission and connection recovery."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from oea import Runtime, MCPServerConfig, RuntimeFault


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"


def payload(result):
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class MCPResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_reads_share_tool_capacity_and_unknown_reads_stay_held(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), max_connections=2, max_concurrency=1))
                await rt.mcp.connect("fixture", "reader")
                await rt.mcp.connect("fixture", "tool")
                tool, read = await asyncio.gather(
                    rt.mcp.call("fixture", "timed_echo", {"delay": .2}, consumer_id="tool", idempotency_key="call"),
                    rt.mcp.read_resource("fixture", "fixture://timed/0.2", consumer_id="reader"))
                values = [payload(tool), json.loads(read["contents"][0]["text"])]
                first, second = sorted(values, key=lambda value: value["started"])
                self.assertLessEqual(first["ended"], second["started"])
                self.assertEqual(len(await rt.mcp.calls("fixture")), 2)
                await rt.mcp.disconnect("fixture")
                config = rt.mcp.configs["fixture"].model_copy(update={"timeout_seconds": .15})
                await rt.mcp.configure(config)
                with self.assertRaises(RuntimeFault) as failure:
                    await rt.mcp.read_resource("fixture", "fixture://timed/2", consumer_id="reader")
                call_id = failure.exception.details["call_id"]
                self.assertIn(call_id, rt.resources.isolated)
                await rt.mcp.disconnect("fixture")
                await rt.mcp.reconcile_capacity(call_id, confirmed_stopped=True, evidence="Owned fixture transport and process closed")

    async def test_consumers_cannot_multiply_service_capacity_and_reinitialize_after_eviction(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), max_connections=3, max_concurrency=1))
                await rt.mcp.connect("fixture", "catalog")
                await rt.mcp.configure_policy("fixture", {"max_service_calls": 2, "state_mode": "reinitialize",
                    "initialization": [{"tool_name": "select_project", "arguments": {"project": "selected"}, "connection_state_only": True}]})
                results = await asyncio.gather(*(rt.mcp.call("fixture", "timed_echo", {"delay": .7},
                    consumer_id="reader-" + str(index), idempotency_key="call") for index in range(7)))
                values = [payload(item) for item in results]
                self.assertTrue(all(item["project"] == "selected" for item in values))
                timeline = sorted([(item["started"], 1) for item in values] + [(item["ended"], -1) for item in values])
                active = peak = 0
                for _, delta in timeline:
                    active += delta
                    peak = max(peak, active)
                self.assertEqual(peak, 2)
                self.assertLessEqual(len(rt.mcp.connections), 3)
                replay = await rt.mcp.call("fixture", "timed_echo", {"delay": .7}, consumer_id="reader-0", idempotency_key="call")
                self.assertTrue(replay["reused"])
                result = await rt.mcp.call("fixture", "echo", {"text": "after eviction"}, consumer_id="reader-0", idempotency_key="next")
                self.assertEqual(payload(result)["project"], "selected")
                self.assertNotEqual(payload(result)["pid"], values[0]["pid"])
                policy = await rt.mcp.policy("fixture")
                self.assertTrue(all(item["schema_fingerprint"] for item in policy["connections"]))

    async def test_timeout_keeps_capacity_across_restart_until_stop_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), max_connections=1, timeout_seconds=.15))
                await rt.mcp.connect("fixture", "reader")
                with self.assertRaises(RuntimeFault):
                    await rt.mcp.call("fixture", "delay_echo", {"text": "late", "delay": 2}, consumer_id="reader", idempotency_key="unknown")
                call = (await rt.mcp.calls())[0]
                self.assertEqual(call["state"], "FAILED")
                self.assertIn(call["id"], rt.resources.isolated)
                client = next(iter(rt.mcp.connections.values()))
                await rt.mcp.disconnect("fixture")
                self.assertTrue(client.closed)
                self.assertIn(call["id"], rt.resources.isolated)
            async with Runtime.open(temp) as rt:
                self.assertIn(call["id"], rt.resources.isolated)
                view = await rt.resources.inspect()
                held = next(item for item in view["capacities"] if item["key"] == "mcp:service:fixture")
                self.assertEqual(held["used"], 1)
                with self.assertRaises(RuntimeFault):
                    await rt.mcp.reconcile_capacity(call["id"], confirmed_stopped=False, evidence="unknown")
                await rt.mcp.reconcile_capacity(call["id"], confirmed_stopped=True, evidence="Owned STDIO server closed and host exited")
                self.assertNotIn(call["id"], rt.resources.isolated)
                with self.assertRaises(RuntimeFault) as no_replay:
                    await rt.mcp.call("fixture", "delay_echo", {"text": "late", "delay": 2}, consumer_id="reader", idempotency_key="unknown")
                self.assertEqual(no_replay.exception.code, "MCP_RESULT_NOT_RETAINED")
                result = await rt.mcp.call("fixture", "echo", {"text": "next"}, consumer_id="reader", idempotency_key="next")
                self.assertEqual(payload(result)["text"], "next")
