"""Real local MCP transports verify service-wide admission and connection recovery."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from oea import Runtime, MCPServerConfig, RuntimeFault, AgentDescriptor, AdapterEvent, ProbeResult, TaskResult


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"


def payload(result):
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class MediatedWorker:
    def describe(self):
        return AgentDescriptor(agent_id="worker")

    async def execute(self, request, context):
        catalog = await context.list_tools()
        tool = next(item["function"]["name"] for item in catalog["tools"] if "timed_increment" in item["function"]["name"])
        result = await context.invoke_tool(tool, {"delay": .6}, "one-mutation")
        yield AdapterEvent(type="completed", result=TaskResult(output=result))

    async def probe(self, reference):
        return ProbeResult(state="STOPPED", confirmed_stopped=True)

    async def aclose(self):
        pass


class MCPResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_late_mutation_settles_mediated_receipt_without_replaying_or_promoting_task(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp, adapters={"worker": MediatedWorker()}) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), max_connections=2, timeout_seconds=.4, persist_results=True))
                await rt.mcp.configure_policy("fixture", {"state_mode": "stateless"})
                await rt.mcp.connect("fixture", "catalog")
                await rt.agents.configure({"id": "worker", "name": "Worker", "kind": "external",
                    "project_id": "default", "mcp_bindings": [{"server_id": "fixture", "tools": ["timed_increment"]}]})
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                task = await rt.threads.turn(thread.id, "Execute one fixture mutation", "original")
                with self.assertRaises(RuntimeFault):
                    await rt.tasks.result(task.id, timeout=5)
                before = await rt.tasks.get(task.id)
                self.assertEqual(before.state, "UNCERTAIN")
                original_call = next(item for item in await rt.mcp.calls() if item["tool_name"] == "timed_increment")
                await rt.mcp.call("fixture", "echo", {"text": "continue"}, consumer_id="next", idempotency_key="next")
                await rt.mcp.capacity.drain()
                from ma_runtime.store import one
                receipt = await rt.store.call(lambda db: one(db,
                    "SELECT * FROM tool_calls WHERE run_id=? AND idempotency_key='one-mutation'", (before.runs[0].id,)))
                self.assertEqual(receipt.state, "SUCCEEDED")
                self.assertEqual(payload(json.loads(receipt.result_json))["counter"], 1)
                self.assertNotIn(before.runs[0].id + ":" + receipt.id, rt.resources.isolated)
                self.assertEqual((await rt.tasks.get(task.id)).state, "UNCERTAIN")
                self.assertFalse(rt.uncertain_slots)
                exact = await rt.mcp.get_call(original_call["id"])
                self.assertEqual(payload(exact["result"])["counter"], 1)
                self.assertNotIn("resource_calls", await rt.tasks.trace(task.id))
                trace = await rt.tasks.trace(task.id, include_resources=True)
                self.assertEqual(trace["resource_calls"][0]["resource_state"], "RELEASED")
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
                await rt.mcp.configure_policy("fixture", {"max_service_calls": 1, "recovery_timeout_seconds": 0})
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
                await rt.mcp.configure_policy("fixture", {"max_service_calls": 1, "recovery_timeout_seconds": 0})
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
                self.assertNotIn(client.resource_owner, rt.resources.owner_keys)
            async with Runtime.open(temp) as rt:
                self.assertIn(call["id"], rt.resources.isolated)
                view = await rt.resources.inspect()
                held = next(item for item in view["capacities"] if item["key"] == "mcp:service:fixture")
                self.assertEqual(held["used"], 1)
                connections = next(item for item in view["capacities"] if item["key"] == "mcp:connections:fixture")
                self.assertEqual(connections["used"], 0)
                with self.assertRaises(RuntimeFault):
                    await rt.mcp.reconcile_capacity(call["id"], confirmed_stopped=False, evidence="unknown")
                await rt.mcp.reconcile_capacity(call["id"], confirmed_stopped=True, evidence="Owned STDIO server closed and host exited")
                self.assertNotIn(call["id"], rt.resources.isolated)
                with self.assertRaises(RuntimeFault) as no_replay:
                    await rt.mcp.call("fixture", "delay_echo", {"text": "late", "delay": 2}, consumer_id="reader", idempotency_key="unknown")
                self.assertEqual(no_replay.exception.code, "MCP_RESULT_NOT_RETAINED")
                result = await rt.mcp.call("fixture", "echo", {"text": "next"}, consumer_id="reader", idempotency_key="next")
                self.assertEqual(payload(result)["text"], "next")

    async def test_idle_close_does_not_hold_the_global_registry_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                for sid in ("pool", "independent"):
                    await rt.mcp.configure(MCPServerConfig(id=sid, name=sid, command=sys.executable,
                        args=("-B", str(FIXTURE)), max_connections=1))
                await rt.mcp.configure_policy("pool", {"state_mode": "stateless"})
                first = await rt.mcp.call("pool", "echo", {"text": "first"}, consumer_id="one", idempotency_key="first")
                old = next(client for (sid, _), client in rt.mcp.connections.items() if sid == "pool")
                close = old.aclose
                entered, release = asyncio.Event(), asyncio.Event()
                async def delayed_close(*, force=False):
                    entered.set()
                    await release.wait()
                    await close(force=force)
                old.aclose = delayed_close
                replacement = asyncio.create_task(rt.mcp.call("pool", "echo", {"text": "second"},
                                                            consumer_id="two", idempotency_key="second"))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    independent = await asyncio.wait_for(rt.mcp.call("independent", "echo", {"text": "independent"},
                        consumer_id="other", idempotency_key="independent"), 5)
                    self.assertEqual(payload(independent)["text"], "independent")
                finally:
                    release.set()
                second = await asyncio.wait_for(replacement, 5)
                self.assertNotEqual(payload(first)["pid"], payload(second)["pid"])
                self.assertNotIn(old.resource_owner.partition(":generation_")[0], rt.resources.capacities)

    async def test_late_terminal_response_automatically_releases_single_service_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), max_connections=2, timeout_seconds=.4, persist_results=True))
                await rt.mcp.connect("fixture", "reader")
                with self.assertRaises(RuntimeFault) as failure:
                    await rt.mcp.call("fixture", "timed_echo", {"delay": .6}, consumer_id="reader", idempotency_key="late")
                self.assertEqual(failure.exception.code, "MCP_TIMEOUT")
                call = (await rt.mcp.calls())[0]
                self.assertIn(call["id"], rt.resources.isolated)
                next_result = await rt.mcp.call("fixture", "echo", {"text": "after late response"},
                                                consumer_id="next", idempotency_key="next")
                self.assertEqual(payload(next_result)["text"], "after late response")
                self.assertNotIn(call["id"], rt.resources.isolated)
                replay = await rt.mcp.call("fixture", "timed_echo", {"delay": .6}, consumer_id="reader", idempotency_key="late")
                self.assertTrue(replay["reused"])
                self.assertEqual(len(await rt.mcp.calls()), 2)
                self.assertTrue(all(row["state"] == "SUCCEEDED" for row in await rt.mcp.calls()))
                exact = await rt.mcp.get_call(server_id="fixture", consumer_id="reader", idempotency_key="late")
                self.assertEqual(exact["call_id"], call["id"])
                self.assertEqual(exact["resource_state"], "RELEASED")
                self.assertTrue(exact["result_retained"])
                recovery = await rt.resources.recover(call["id"], "recover-original")
                again = await rt.resources.recover(call["id"], "recover-original")
                self.assertEqual(recovery["id"], again["id"])
                self.assertEqual(again["state"], "RELEASED")
                self.assertTrue(again["reused"])
            async with Runtime.open(temp) as rt:
                self.assertNotIn(call["id"], rt.resources.isolated)
                replay = await rt.mcp.call("fixture", "timed_echo", {"delay": .6}, consumer_id="reader", idempotency_key="late")
                self.assertTrue(replay["reused"])

    async def test_upstream_error_has_safe_details_and_exact_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), persist_results=True))
                result = await rt.mcp.call("fixture", "structured_failure", {}, consumer_id="reader", idempotency_key="rejection")
                self.assertTrue(result["isError"])
                call = await rt.mcp.get_call(result["call_id"])
                self.assertEqual(call["state"], "FAILED")
                self.assertEqual(call["error"]["code"], "MCP_TOOL_ERROR")
                self.assertEqual(call["error"]["details"]["upstream_code"], "SAMPLE_REJECTED")
                self.assertEqual(call["error"]["details"]["path"], ["query", "where"])
                self.assertEqual(call["resource_state"], "RELEASED")

    async def test_cancelled_caller_keeps_original_request_until_terminal_response(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure(MCPServerConfig(id="fixture", name="Fixture", command=sys.executable,
                    args=("-B", str(FIXTURE)), max_connections=2, timeout_seconds=2))
                await rt.mcp.connect("fixture", "reader")
                pending = asyncio.create_task(rt.mcp.call("fixture", "timed_echo", {"delay": .3},
                                                         consumer_id="reader", idempotency_key="cancel"))
                async with asyncio.timeout(3):
                    while not any(request.dispatched for client in rt.mcp.connections.values() for request in client.operations.values()):
                        await asyncio.sleep(.01)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                original = (await rt.mcp.calls())[0]
                self.assertIn(original["id"], rt.resources.isolated)
                result = await rt.mcp.call("fixture", "echo", {"text": "continued"},
                                           consumer_id="next", idempotency_key="next")
                self.assertEqual(payload(result)["text"], "continued")
                self.assertNotIn(original["id"], rt.resources.isolated)
                replay = await rt.mcp.call("fixture", "timed_echo", {"delay": .3}, consumer_id="reader", idempotency_key="cancel")
                self.assertTrue(replay["reused"])
