"""Controller business-tool trials use actual MCP and the common action gateway."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import MCPServer
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one
from test_upstream_mcp_v2 import SERVER, content


class NoModel:
    async def complete(self, *args, **kwargs):
        raise AssertionError("Controller tool trials must not invoke a model")


class ControllerCalls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-controller-tools-")
        self.root = Path(self.temporary.name)
        script = self.root / "business.py"
        extra = '\nfrom pydantic import BaseModel\nclass LargeResult(BaseModel):\n    body: str\n@mcp.tool()\nasync def large(size: int) -> LargeResult:\n    return LargeResult(body="x" * size)\n@mcp.tool()\nasync def raw_large(size: int) -> dict:\n    return {"body": "x" * size}\n'
        script.write_text(SERVER.replace('if __name__ == "__main__":', extra + '\nif __name__ == "__main__":'), encoding="utf-8")
        self.host = await Host(self.root / "state", model_api=NoModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        await self.host.mcp.save(MCPServer(id="business", project_id="p", session_scope="shared", command=(sys.executable, "-B", str(script)),
            environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TEMP": os.environ["TEMP"], "TMP": os.environ["TMP"], "EFFECT_FILE": str(self.root / "effect.txt")}, timeout_seconds=30))
        await self.host.mcp.discover("business", "p")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def call(self, tool="echo", *, key="original", value=37, wait=True):
        return await self.host.mcp.call("business", tool, {"value": value}, key, project_id="p", consumer_id="controller", wait=wait)

    async def count(self, table):
        return await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"])

    async def test_thirty_two_duplicate_trials_have_one_real_effect_and_no_agent_or_model(self):
        receipts = await asyncio.gather(*(self.call() for _ in range(32)))
        identities = {item["id"] for item in receipts}
        self.assertEqual(len(identities), 1)
        identity = identities.pop()
        result = await self.host.mcp.call_result(identity, project_id="p", consumer_id="controller")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(content(result["result"]), {"value": 37, "count": 1})
        self.assertTrue(result["stop_confirmed"])
        for table in ("agent_definitions", "instances", "runs", "usage", "budgets"):
            self.assertEqual(await self.count(table), 0)
        self.assertEqual(await self.count("actions"), 1)
        self.assertEqual(await self.count("tool_intents"), 1)
        history = await self.host.mcp.calls("business", "p")
        self.assertEqual(history["items"][0]["intent_id"], identity)
        self.assertIsNone(history["items"][0]["run_id"])
        self.assertEqual(history["items"][0]["tool_name"], "echo")
        with self.assertRaises(RuntimeFault) as conflict:
            await self.call(value=99)
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        for project, consumer in (("q", "controller"), ("p", "another")):
            with self.assertRaises(RuntimeFault):
                await self.host.mcp.call_result(identity, project_id=project, consumer_id=consumer)
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"enabled": False}), expected_revision=1)
        replay = await self.call()
        self.assertEqual(replay["action_id"], result["action_id"])
        self.assertEqual(replay["result"], result["result"])
        self.assertEqual(self.host.mcp.connections, {})

    async def test_invalid_tool_parameters_create_no_intent_or_external_call(self):
        with self.assertRaises(RuntimeFault) as invalid:
            await self.call(value="invalid")
        self.assertEqual(invalid.exception.code, "INVALID_REQUEST")
        self.assertEqual(await self.count("tool_intents"), 0)
        self.assertEqual(await self.count("actions"), 0)
        connection = await self.host.mcp.connection("business", 1)
        self.assertEqual(content(await connection.session.call_tool("finish"))["count"], 0)

    async def test_accepted_input_survives_the_gap_before_action_creation_and_restart(self):
        with patch.object(self.host.tools, "invoke_intent", side_effect=RuntimeError("Interrupted after acceptance")):
            with self.assertRaises(RuntimeError):
                await self.call(key="accepted-gap")
        identity = await self.host.store.call(lambda db: one(db, "SELECT id FROM tool_intents")["id"])
        original = await self.host.mcp.call_result(identity, project_id="p", consumer_id="controller")
        self.assertIsNone(original["action_id"])
        self.assertEqual(original["arguments"], {"value": 37})
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=NoModel()).__aenter__()
        retained = await self.host.mcp.call_result(identity, project_id="p", consumer_id="controller")
        self.assertEqual(retained["arguments"], original["arguments"])
        self.assertEqual(self.host.mcp.connections, {})
        result = await self.call(key="accepted-gap")
        self.assertEqual(result["id"], identity)
        self.assertEqual(content(result["result"]), {"value": 37, "count": 1})
        self.assertEqual(await self.count("tool_intents"), 1)
        self.assertEqual(await self.count("actions"), 1)
        self.assertIsNone(await self.host.store.call(lambda db: one(db, "SELECT input_json FROM actions WHERE intent_id=?", (identity,))["input_json"]))

    async def test_cancel_before_dispatch_returns_known_receipt_to_original_waiter(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.host.mcp.authorize_intent
        async def authorize(intent_id, name):
            entered.set()
            await release.wait()
            await original(intent_id, name)
        self.host.mcp.authorize_intent = authorize
        pending = asyncio.create_task(self.call())
        await asyncio.wait_for(entered.wait(), 3)
        identity = await self.host.store.call(lambda db: one(db, "SELECT id FROM tool_intents")["id"])
        cancelled = await self.host.mcp.cancel_call(identity, project_id="p", consumer_id="controller")
        self.assertEqual(cancelled["state"], "failed")
        self.assertEqual(cancelled["error"]["code"], "ACTION_NOT_STARTED")
        self.assertTrue(cancelled["stop_confirmed"])
        self.assertEqual((await pending)["action_id"], cancelled["action_id"])
        connection = await self.host.mcp.connection("business", 1)
        self.assertEqual(content(await connection.session.call_tool("finish"))["count"], 0)

    async def test_twenty_live_trials_cancel_honestly_and_retire_only_after_original_outcomes(self):
        receipts = await asyncio.gather(*(self.call("hold", key="hold-" + str(index), wait=False) for index in range(20)))
        connection = await self.host.mcp.connection("business", 1)
        self.assertEqual(content(await asyncio.wait_for(connection.session.call_tool("observe_ready"), 15)), {"active": 20, "peak": 20})
        with self.assertRaises(RuntimeFault) as busy:
            await self.host.mcp.disconnect("business", "p")
        self.assertEqual(busy.exception.code, "MCP_IN_USE")
        cancelled = await self.host.mcp.cancel_call(receipts[0]["id"], project_id="p", consumer_id="controller")
        self.assertEqual(cancelled["state"], "needs_review")
        self.assertTrue(cancelled["in_flight"])
        self.assertFalse(cancelled["stop_confirmed"])
        with self.assertRaises(RuntimeFault) as live:
            await self.host.mcp.confirm_call(cancelled["id"], "cannot-confirm", "Original call is still running", "not_applied", project_id="p", consumer_id="controller")
        self.assertEqual(live.exception.code, "ACTION_IN_FLIGHT")
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"name": "Changed"}), expected_revision=1)
        self.assertFalse(connection.task.done())
        self.assertEqual(content(await connection.session.call_tool("finish"))["count"], 20)
        await asyncio.gather(*tuple(self.host.tools.inflight.values()))
        if self.host.mcp.retirement is not None:
            await self.host.mcp.retirement
        final = await self.host.mcp.call_result(cancelled["id"], project_id="p", consumer_id="controller")
        self.assertEqual(final["state"], "completed")
        self.assertIsNotNone(final["cancel_requested_at"])
        self.assertTrue(final["stop_confirmed"])
        self.assertEqual(content(final["result"])["count"], 20)
        self.assertTrue(connection.task.done())
        self.assertEqual(await self.count("actions"), 20)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(DISTINCT operation_key) AS count FROM actions")["count"]), 20)

    async def test_actual_write_then_disconnect_restart_and_confirmation_never_replays(self):
        original = await self.call("disconnect")
        self.assertEqual(original["state"], "needs_review")
        self.assertEqual(original["error"]["code"], "OUTCOME_UNCERTAIN")
        self.assertEqual((self.root / "effect.txt").read_text(encoding="utf-8"), "37")
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=NoModel()).__aenter__()
        replay = await self.call("disconnect")
        self.assertEqual(replay["action_id"], original["action_id"])
        self.assertEqual(replay["state"], "needs_review")
        self.assertEqual(self.host.mcp.connections, {})
        with self.assertRaises(RuntimeFault) as wrong_owner:
            await self.host.runs.confirm_action(original["action_id"], "p", "wrong-owner", "This is a controller call", "not_applied")
        self.assertEqual(wrong_owner.exception.code, "PERMISSION_DENIED")
        actual = {"isError": False, "content": [{"type": "text", "text": "37"}], "structured_content": {"value": 37}}
        await self.host.mcp.confirm_call(original["id"], "verified", "Read the actual original external value", "completed", project_id="p", consumer_id="controller", result=actual)
        confirmed = await self.call("disconnect")
        self.assertEqual(confirmed["state"], "completed")
        self.assertEqual(confirmed["result"], actual)
        with self.assertRaises(RuntimeFault) as changed:
            await self.host.mcp.call_result(original["id"], project_id="p", consumer_id="controller", version=original["version"])
        self.assertEqual(changed.exception.code, "SOURCE_VERSION_CHANGED")
        self.assertEqual((self.root / "effect.txt").read_text(encoding="utf-8"), "37")
        self.assertEqual(await self.count("action_resolutions"), 1)

    async def test_large_controller_result_reference_reads_original_owner_without_another_call(self):
        for tool_name, pointer in (("large", "/result/structured_content/body"), ("raw_large", "/result/content/0/text")):
            params = {"server_id": "business", "tool_name": tool_name, "arguments": {"size": 10000}, "idempotency_key": tool_name, "project_id": "p"}
            value = await self.host.registry.call("mcp.call", params, consumer_id="large-controller")
            self.assertEqual(value["state"], "completed", value["error"])
            self.assertFalse(value["result"]["isError"])
            reference = await self.host.results.issue("mcp.call", params, value, "large-controller", characters=len(json.dumps(value)))
            self.assertIsNotNone(reference)
            parts, offset = [], 0
            while True:
                page = await self.host.results.read(reference["result_ref"], project_id="p", consumer_id="large-controller", json_pointer=pointer, offset=offset)
                parts.append(page["text"])
                if page["complete"]:
                    break
                offset = page["next_offset"]
            original = json.loads("".join(parts))
            self.assertEqual(json.loads(original)["body"] if tool_name == "raw_large" else original, "x" * 10000)
        self.assertEqual(await self.count("actions"), 2)
        with self.assertRaises(RuntimeFault) as spoof:
            await self.host.registry.call("mcp.call_result", {"call_id": value["id"], "project_id": "p", "consumer_id": "large-controller"}, consumer_id="other")
        self.assertEqual(spoof.exception.code, "PERMISSION_DENIED")
