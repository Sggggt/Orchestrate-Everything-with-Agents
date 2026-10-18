"""Bidirectional supervision contracts with temporary state and scripted models."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ma_runtime import AgentConfig, AgentDescriptor, PlatformClient, Runtime, RuntimeConfig
from ma_runtime.bridges.compact import CompactBridge
from ma_runtime.bridges.dispatch import dispatch
from ma_runtime.errors import RuntimeFault
from ma_runtime.web.app import create_app
from test_skill_system import PolicyLLM


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-operations-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root, RuntimeConfig(max_concurrency=3, reserved_external_slots=0)).__aenter__()
        self.session = await self.rt.sessions.create(title="Operations contracts")
        await self.rt.operations.bind(self.session.id, "coordinator")

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def agent(self, identity="worker", actions=(), **changes):
        config = AgentConfig(id=identity, name=identity, kind="llm", base_url="http://model.invalid/v1", model="scripted", **changes)
        await self.rt.agents.configure(config)
        adapter = PolicyLLM(config, actions)
        self.rt.adapters[identity] = adapter
        thread = await self.rt.threads.create(self.session.id, identity)
        return config, adapter, thread

    async def state(self, task_id, target):
        for _ in range(300):
            task = await self.rt.tasks.get(task_id)
            if task.state == target:
                return task
            if task.state in {"FAILED", "CANCELLED", "UNCERTAIN"}:
                self.fail(f"Task ended with {task.state}: {task.error}")
            await asyncio.sleep(0.01)
        self.fail(f"Task did not reach {target}")

    async def test_compact_inbox_detail_ack_restart_and_idempotency(self):
        details = "Detailed evidence. " * 200
        report = await self.rt.operations.report(self.session.id, "Need inspection", "report-key", details=details)
        repeated = await self.rt.operations.report(self.session.id, "Need inspection", "report-key", details=details)
        self.assertEqual(repeated["report_id"], report["report_id"])
        self.assertTrue(repeated["reused"])
        inbox = await self.rt.operations.inbox(self.session.id, "coordinator")
        self.assertEqual(len(inbox["items"]), 1)
        self.assertNotIn(details, json.dumps(inbox))
        self.assertEqual((await self.rt.operations.read(self.session.id, report["event_id"]))["report"]["details"], details)
        await self.rt.operations.ack(self.session.id, "coordinator", inbox["cursor"])
        with self.assertRaises(RuntimeFault):
            await self.rt.operations.ack(self.session.id, "coordinator", inbox["cursor"] + 1)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.operations.inbox(self.session.id, "coordinator", cursor=999999999)
        self.assertEqual(error.exception.code, "RESYNC_REQUIRED")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root).__aenter__()
        self.assertEqual((await self.rt.operations.inbox(self.session.id, "coordinator"))["items"], [])
        with self.assertRaises(RuntimeFault):
            await self.rt.operations.report(self.session.id, "Changed", "report-key")

    async def test_long_wait_wakes_from_notification_and_timeout_is_quiet(self):
        waiter = asyncio.create_task(self.rt.operations.inbox(self.session.id, "coordinator", timeout=2))
        await asyncio.sleep(0.02)
        await self.rt.operations.report(self.session.id, "New information", "wake")
        value = await asyncio.wait_for(waiter, 1)
        self.assertFalse(value["timed_out"])
        await self.rt.operations.ack(self.session.id, "coordinator", value["cursor"])
        self.assertTrue((await self.rt.operations.inbox(self.session.id, "coordinator", timeout=0.02))["timed_out"])

    async def test_empty_wait_does_not_periodically_query_the_inbox(self):
        with patch.object(self.rt.operations, "_feed", wraps=self.rt.operations._feed) as feed:
            value = await self.rt.operations.inbox(self.session.id, "coordinator", timeout=0.25)
            self.assertTrue(value["timed_out"])
            self.assertEqual(feed.call_count, 1)

    async def test_child_requests_guidance_under_none_and_resumes_same_task(self):
        _, adapter, thread = await self.agent(actions=(("report_issue", {"summary": "Tool setup needs guidance", "details": "No external action has run.", "wait": True}),), collaboration=False)
        task = await self.rt.threads.turn(thread.id, "Inspect", "guidance", tool_policy={"mode": "none"})
        waiting = await self.state(task.id, "WAITING")
        await asyncio.wait_for(asyncio.gather(*(active.driver for active in list(self.rt.active.values()) if active.driver)), 1)
        self.assertFalse(self.rt.active)
        inbox = await self.rt.operations.inbox(self.session.id, "coordinator")
        item = next(item for item in inbox["items"] if item["type"] == "operations.reported")
        snapshot = await self.rt.sessions.snapshot(self.session.id, presentation=True)
        self.assertEqual(snapshot["attention_thread_ids"], [thread.id])
        self.assertNotIn("No external action has run.", json.dumps(snapshot))
        await self.rt.operations.ack(self.session.id, "coordinator", inbox["cursor"])
        self.assertEqual((await self.rt.sessions.snapshot(self.session.id, presentation=True))["attention_thread_ids"], [thread.id])
        other = await self.rt.sessions.create(title="Other session")
        self.assertEqual((await self.rt.sessions.snapshot(other.id, presentation=True))["attention_thread_ids"], [])
        reply = await self.rt.operations.reply(item["report_id"], "coordinator", "Use the saved connection and verify its selection.", "reply-key")
        repeated = await self.rt.operations.reply(item["report_id"], "coordinator", "Use the saved connection and verify its selection.", "reply-key")
        self.assertEqual(reply["message_id"], repeated["message_id"])
        self.assertTrue(repeated["reused"])
        await self.rt.tasks.result(task.id, timeout=10)
        result = await self.rt.tasks.get(task.id)
        self.assertEqual(result.id, waiting.id)
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual((await self.rt.sessions.snapshot(self.session.id, presentation=True))["attention_thread_ids"], [])
        self.assertIn("Use the saved connection", json.dumps(adapter.captured[-1]["messages"]))
        self.assertIn("report_issue", [tool["function"]["name"] for tool in adapter.captured[0]["tools"]])

    async def test_guidance_request_and_reply_survive_host_restart(self):
        config, _, thread = await self.agent(actions=(("report_issue", {"summary": "Need guidance", "wait": True}),))
        task = await self.rt.threads.turn(thread.id, "Inspect", "restart-guidance")
        await self.state(task.id, "WAITING")
        report = (await self.rt.operations.inbox(self.session.id, "coordinator"))["items"][0]
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root).__aenter__()
        self.rt.adapters[config.id] = PolicyLLM(config)
        await self.rt.operations.reply(report["report_id"], "coordinator", "Continue with the original operation key.", "after-restart")
        await self.rt.tasks.result(task.id, timeout=10)

    async def test_guidance_respects_existing_message_budget(self):
        _, _, thread = await self.agent(actions=(("report_issue", {"summary": "Need guidance", "wait": True}),))
        task = await self.rt.threads.turn(thread.id, "Inspect", "message-budget")
        await self.state(task.id, "WAITING")
        report = (await self.rt.operations.inbox(self.session.id, "coordinator"))["items"][0]
        self.rt.config.max_message_bytes = 1024
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.operations.reply(report["report_id"], "coordinator", "x" * 2048, "too-large")
        self.assertEqual(error.exception.code, "PAYLOAD_TOO_LARGE")
        self.assertIsNone((await self.rt.operations.read(self.session.id, report["event_id"]))["report"]["reply_message_id"])
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "WAITING")

    async def test_reports_and_receivers_cannot_cross_session_scope(self):
        report = await self.rt.operations.report(self.session.id, "Need inspection", "scope")
        other = await self.rt.sessions.create(title="Other session")
        await self.rt.operations.bind(other.id, "other-consumer")
        with self.assertRaises(RuntimeFault):
            await self.rt.operations.read(other.id, report["event_id"])
        with self.assertRaises(RuntimeFault):
            await self.rt.operations.inbox(self.session.id, "other-consumer")
        _, _, thread = await self.agent()
        with self.assertRaises(RuntimeFault):
            await self.rt.operations.bind(other.id, "other-consumer", thread_id=thread.id)

    async def test_internal_notifications_coalesce_and_do_not_recurse(self):
        config, _, thread = await self.agent("supervisor", enabled=False)
        await self.rt.operations.bind(self.session.id, "internal", thread_id=thread.id)
        await self.rt.operations.report(self.session.id, "First report", "first")
        await self.rt.operations.report(self.session.id, "Second report", "second")
        messages = await self.rt.messages.list(self.session.id)
        self.assertEqual(len(messages.items), 1)
        self.assertEqual(len((await self.rt.operations.inbox(self.session.id, "internal"))["items"]), 2)
        # Enable the coordinator but leave its model missing so activation is
        # queued and can be inspected without an external provider call.
        await self.rt.agents.configure(config.model_copy(update={"enabled": True, "base_url": "", "model": ""}))
        await self.rt.operations.report(self.session.id, "Third report", "third")
        await self.rt.operations.report(self.session.id, "Fourth report", "fourth")
        tasks = await self.rt.tasks.list(self.session.id)
        self.assertEqual(len(tasks.items), 1)
        self.assertEqual(tasks.items[0].spec.metadata["purpose"], "operations_notice")

    async def test_coordinator_wait_is_interrupted_without_fake_completion(self):
        child_config, child, child_thread = await self.agent("child", actions=(("report_issue", {"summary": "Need project selection", "wait": True}),), enabled=False)
        child_task = await self.rt.threads.turn(child_thread.id, "Inspect", "child-wait")
        _, parent, parent_thread = await self.agent("parent", actions=(("wait_for_tasks", {"task_ids": [child_task.id]}),))
        await self.rt.operations.bind(self.session.id, "internal", thread_id=parent_thread.id)
        parent_task = await self.rt.threads.turn(parent_thread.id, "Coordinate", "parent-wait")
        await self.state(parent_task.id, "WAITING")
        self.rt.agent_configs[child_config.id] = child_config.model_copy(update={"enabled": True})
        self.rt.notify()
        await self.rt.tasks.result(parent_task.id, timeout=10)
        await self.state(child_task.id, "WAITING")
        self.assertTrue(any('"operations_available":true' in (entry.get("content") or "") for entry in parent.captured[-1]["messages"]))
        self.assertIn("pending_wait", json.dumps(parent.captured[-1]["messages"]))
        self.assertIn("operations", [tool["function"]["name"] for tool in parent.captured[-1]["tools"]])
        report = next(item for item in (await self.rt.operations.inbox(self.session.id, "internal"))["items"] if item["report_id"])
        await self.rt.operations.reply(report["report_id"], "internal", "Select the saved project.", "guide-child")
        await self.rt.tasks.result(child_task.id, timeout=10)

    async def test_failed_child_returns_error_to_waiting_parent(self):
        child_config, _, child_thread = await self.agent("failing", enabled=False)
        class Failing(PolicyLLM):
            async def _completion(inner, messages, context, tools=None):
                raise RuntimeFault("ADAPTER_PROTOCOL_ERROR", "Synthetic protocol failure")
                yield
        self.rt.adapters[child_config.id] = Failing(child_config)
        child = await self.rt.threads.turn(child_thread.id, "Inspect", "failing-child")
        _, parent, parent_thread = await self.agent("parent", actions=(("wait_for_tasks", {"task_ids": [child.id]}),))
        task = await self.rt.threads.turn(parent_thread.id, "Coordinate", "failed-parent-wait")
        await self.state(task.id, "WAITING")
        self.rt.agent_configs[child_config.id] = child_config.model_copy(update={"enabled": True})
        self.rt.notify()
        await self.rt.tasks.result(task.id, timeout=10)
        self.assertIn("ADAPTER_PROTOCOL_ERROR", json.dumps(parent.captured[-1]["messages"]))
        inbox = await self.rt.operations.inbox(self.session.id, "coordinator")
        self.assertTrue(any(item["type"] == "task.failed" for item in inbox["items"]))

    async def test_diagnostic_uses_normal_instances_and_stable_receipt(self):
        await self.agent()
        test = await self.rt.operations.test(self.session.id, "worker", "Run a bounded inspection.", "test-key")
        repeated = await self.rt.operations.test(self.session.id, "worker", "Run a bounded inspection.", "test-key")
        self.assertEqual(test["task_id"], repeated["task_id"])
        await self.rt.tasks.result(test["task_id"], timeout=10)
        task = await self.rt.tasks.get(test["task_id"])
        self.assertEqual(task.spec.metadata["purpose"], "operations_test")
        self.assertEqual(len((await self.rt.tasks.list(self.session.id)).items), 1)
        self.assertTrue(any(item["type"] == "operations.test_completed" for item in (await self.rt.operations.inbox(self.session.id, "coordinator"))["items"]))

    async def test_jsonl_child_and_coordinator_exchange_guidance(self):
        descriptor = AgentDescriptor(agent_id="guidance-worker", execution_mode="subprocess", cancellation="terminate_owned_process",
            supports_suspend=True, checkpoint_mode="opaque_json", checkpoint_format_version="guidance-v1", tool_mediation="full")
        await self.rt.agents.configure(AgentConfig(id="guidance-worker", name="Guidance worker", kind="process",
            command=(sys.executable, "-B", str(Path(__file__).parent / "fixtures/operations_agent.py")), process_descriptor=descriptor))
        coordinator = await self.rt.threads.create(self.session.id, "guidance-worker", title="Coordinator")
        child = await self.rt.threads.create(self.session.id, "guidance-worker", title="Child")
        await self.rt.operations.bind(self.session.id, "process-main", thread_id=coordinator.id)
        task = await self.rt.threads.turn(child.id, "Inspect", "jsonl-guidance", tool_policy={"mode": "none"})
        result = await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(result.output["task_id"], task.id)
        self.assertEqual(result.output["guidance"], "Continue with the original operation key.")
        self.assertEqual((await self.rt.tasks.get(task.id)).attempt_count, 2)

    async def test_diagnostic_accepts_existing_task_scoped_restrictions(self):
        await self.agent()
        test = await self.rt.operations.test(self.session.id, "worker", "Inspect without business tools.", "restricted-test",
            tool_policy={"mode": "none"}, skill_policy={"mode": "none"})
        await self.rt.tasks.result(test["task_id"], timeout=10)
        task = await self.rt.tasks.get(test["task_id"])
        self.assertEqual(task.spec.tool_policy.mode, "none")
        self.assertEqual(task.spec.skill_policy.mode, "none")
        with self.assertRaises(ValueError):
            await self.rt.operations.test(self.session.id, "worker", "Inspect", "invalid-policy", tool_policy=False)
        self.assertEqual(len((await self.rt.tasks.list(self.session.id)).items), 1)

    async def test_coordinator_rebinding_revokes_running_router(self):
        config, _, thread = await self.agent("supervisor")
        await self.rt.operations.bind(self.session.id, "internal", thread_id=thread.id)
        class Rebound(PolicyLLM):
            async def _completion(inner, messages, context, tools=None):
                await self.rt.operations.bind(self.session.id, "replacement")
                with self.assertRaises(RuntimeFault) as error:
                    await context.operations("inspect")
                self.assertEqual(error.exception.code, "PERMISSION_DENIED")
                yield {"calls": [], "text": "Checked", "finish_reason": "stop"}
        self.rt.adapters[config.id] = Rebound(config)
        task = await self.rt.threads.turn(thread.id, "Inspect", "rebind")
        await self.rt.tasks.result(task.id, timeout=10)

    async def test_readonly_coordinator_can_suspend_without_dispatch_permission(self):
        config, adapter, thread = await self.agent("readonly", actions=(("wait_for_message", {}),), collaboration=False)
        await self.rt.operations.bind(self.session.id, "readonly-main", thread_id=thread.id)
        task = await self.rt.threads.turn(thread.id, "Observe", "readonly-main", tool_policy={"mode": "none"})
        await self.state(task.id, "WAITING")
        await self.rt.operations.report(self.session.id, "Need observation", "readonly-report")
        await self.rt.tasks.result(task.id, timeout=10)
        names = {tool["function"]["name"] for tool in adapter.captured[0]["tools"]}
        self.assertIn("operations", names)
        self.assertIn("wait_for_message", names)
        self.assertNotIn("spawn_instances", names)

    async def test_uncertain_child_is_reported_without_releasing_its_slot(self):
        child_config, _, child_thread = await self.agent("uncertain", enabled=False)
        class Uncertain(PolicyLLM):
            async def _completion(inner, messages, context, tools=None):
                raise RuntimeFault("OUTCOME_UNCERTAIN", "Verify the original call")
                yield
        self.rt.adapters[child_config.id] = Uncertain(child_config)
        child = await self.rt.threads.turn(child_thread.id, "Inspect", "uncertain-child")
        _, parent, parent_thread = await self.agent("parent", actions=(("wait_for_tasks", {"task_ids": [child.id]}),))
        task = await self.rt.threads.turn(parent_thread.id, "Coordinate", "uncertain-parent")
        await self.state(task.id, "WAITING")
        self.rt.agent_configs[child_config.id] = child_config.model_copy(update={"enabled": True})
        self.rt.notify()
        await self.rt.tasks.result(task.id, timeout=10)
        self.assertIn("OUTCOME_UNCERTAIN", json.dumps(parent.captured[-1]["messages"]))
        self.assertEqual((await self.rt.tasks.get(child.id)).state, "UNCERTAIN")
        self.assertTrue(self.rt.uncertain_slots or self.rt._uncertain_owners)

    async def test_coordinator_failure_does_not_schedule_itself_again(self):
        config, _, thread = await self.agent("supervisor")
        await self.rt.operations.bind(self.session.id, "internal", thread_id=thread.id)
        class Failing(PolicyLLM):
            async def _completion(inner, messages, context, tools=None):
                raise RuntimeFault("ADAPTER_PROTOCOL_ERROR", "Coordinator failed")
                yield
        self.rt.adapters[config.id] = Failing(config)
        task = await self.rt.threads.turn(thread.id, "Inspect", "failed-coordinator")
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(len((await self.rt.tasks.list(self.session.id)).items), 1)
        self.assertEqual(len((await self.rt.operations.inbox(self.session.id, "internal"))["items"]), 1)

    async def test_sdk_http_compact_contract_and_small_action_discovery(self):
        app = create_app(runtime=self.rt)
        self.assertFalse(any("/operations" in route.path for route in app.routes))
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                await client.operations.report(self.session.id, "SDK report", "sdk-report")
                self.assertEqual(len((await client.operations.inbox(self.session.id, "coordinator"))["items"]), 1)
        async def invoke(method, params):
            return json.loads(json.dumps(await dispatch(self.rt, method, params)))
        bridge = CompactBridge(invoke)
        guide = bridge.guide("operations", "inbox")
        self.assertNotIn("schemas", guide)
        self.assertEqual(next(item for item in guide["parameters"] if item["name"] == "limit")["default"], 20)
        result = await bridge.call("operations", "inbox", {"session_id": self.session.id, "consumer_id": "coordinator"})
        self.assertTrue(result["ok"])

    async def test_browser_latest_failure_is_independent_of_recent_history(self):
        config = AgentConfig(id="window-worker", name="Window worker", kind="mock")
        await self.rt.agents.configure(config)
        thread = await self.rt.threads.create(self.session.id, config.id)
        failed = await self.rt.tasks.submit({"session_id": self.session.id, "agent_id": config.id,
            "thread_id": thread.id, "payload": {"_mock": {"delay": 0, "fail_attempts": 1}}, "idempotency_key": "window-failure"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(failed.id, timeout=10)
        # Synthetic history belongs only to this temporary workspace. Inserting in
        # one transaction avoids executing hundreds of unrelated Agent runs.
        other = await self.rt.threads.create(self.session.id, config.id)
        def history(db):
            from ma_runtime.models import TaskSpec
            for index in range(300):
                item = self.rt.tasks.insert(db, TaskSpec(session_id=self.session.id, agent_id=config.id,
                    thread_id=other.id, payload={}, idempotency_key=f"history:{index}"))
                db.execute("UPDATE tasks SET state='SUCCEEDED' WHERE id=?", (item.id,))
        await self.rt.store.call(history, transaction=True)
        view = await self.rt.sessions.snapshot(self.session.id, presentation=True)
        self.assertNotIn(failed.id, [task["id"] for task in view["tasks"]])
        self.assertEqual(view["failed_thread_ids"], [thread.id])
        recovered = await self.rt.threads.turn(thread.id, "Recover", "window-recovery")
        await self.rt.tasks.result(recovered.id, timeout=10)
        self.assertEqual((await self.rt.sessions.snapshot(self.session.id, presentation=True))["failed_thread_ids"], [])
