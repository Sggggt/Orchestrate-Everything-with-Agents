"""Budget admission, inherited ownership and retained partial model delivery."""

import asyncio
import sys
import unittest
from pathlib import Path

from oea import BudgetPolicy, RuntimeFault, WorkflowBuilder
from ma_runtime.execution_policy import request_hash
from ma_runtime.models import TaskSpec
from ma_runtime.store import one
import test_model_configuration as model_fixture


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = model_fixture.ModelConfigurationTests.asyncSetUp
    asyncTearDown = model_fixture.ModelConfigurationTests.asyncTearDown
    config = model_fixture.ModelConfigurationTests.config

    async def test_mcp_receipt_changes_do_not_reset_stall_without_new_business_evidence(self):
        await self.rt.mcp.configure({"id": "fixture", "name": "Fixture", "command": sys.executable,
                                     "args": ["-B", str(Path(__file__).parent / "fixtures/mcp_server.py")]})
        await self.rt.agents.configure(self.config(mcp_bindings=[{"server_id": "fixture", "tools": ["echo"]}]))
        session = await self.rt.sessions.create()
        for mode in ("same", "distinct", "truncated"):
            if mode == "truncated":
                await self.rt.mcp.disconnect("fixture")
                await self.rt.mcp.configure(self.rt.mcp.configs["fixture"].model_copy(update={"max_output_bytes": 1024}))
            budget = await self.rt.budgets.create(session.id, {"max_stalled_model_calls": 1,
                "progress_tools": ["mcp:fixture:echo"]}, mode)
            thread = await self.rt.threads.create(session.id, "model-worker")
            task = await self.rt.tasks.submit(TaskSpec(session_id=session.id, agent_id="model-worker", thread_id=thread.id,
                payload={"input": "test", "repeat_read": mode}, idempotency_key=mode), budget_id=budget["id"])
            if mode == "same":
                await self.failure(task.id, "PROGRESS_STALLED")
                self.assertEqual(len(self.captured), 2)
                self.assertEqual((await self.rt.budgets.for_task(task.id))["task_stalled_calls"], 1)
            elif mode == "distinct":
                await self.rt.tasks.result(task.id, timeout=15)
                self.assertEqual(len(self.captured), 5)
            else:
                await self.failure(task.id, "PROGRESS_STALLED")
                self.assertEqual(len(self.captured), 6)
        calls = await self.rt.mcp.calls("fixture")
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call["state"] == "SUCCEEDED" for call in calls))

    async def create(self, policy=None, payload=None, *, native=False):
        await self.rt.agents.configure(self.config(**({"kind": "llm", "command": (), "llm_required": False} if native else {})))
        session = await self.rt.sessions.create()
        budget = await self.rt.budgets.create(session.id, policy or {}, "limits")
        thread = await self.rt.threads.create(session.id, "model-worker")
        spec = TaskSpec(session_id=session.id, thread_id=thread.id, agent_id="model-worker",
                        idempotency_key="task", payload=payload or {"input": "test", "twice": True})
        task = await self.rt.tasks.submit(spec, budget_id=budget["id"])
        return session, budget, spec, task

    async def failure(self, task_id, code):
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task_id, timeout=15)
        task = await self.rt.tasks.get(task_id)
        self.assertEqual(task.state, "FAILED", task.error)
        self.assertEqual(task.error["code"], code)
        return task

    async def test_limit_blocks_transport_and_retry_inherits_usage(self):
        session, budget, spec, task = await self.create({"max_model_calls": 1})
        failed = await self.failure(task.id, "BUDGET_EXHAUSTED")
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(failed.result.output["text"], "first-model:v1")
        usage = await self.rt.budgets.get(budget["id"])
        self.assertEqual(usage["usage"], {"model_calls": 1, "input_tokens": 2, "output_tokens": 3, "tool_calls": 0})
        self.assertEqual(usage["estimated_model_calls"], 0)
        self.assertTrue((await self.rt.tasks.submit(spec, budget_id=budget["id"])).reused)
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.tasks.submit(spec)
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        retry = await self.rt.tasks.retry(task.id, "retry")
        await self.failure(retry.id, "BUDGET_EXHAUSTED")
        self.assertEqual((await self.rt.budgets.for_task(retry.id))["id"], budget["id"])
        self.assertEqual(len(self.captured), 1)

    async def test_missing_usage_keeps_reservation_and_next_output_is_capped(self):
        _, budget, _, task = await self.create({"max_output_tokens": 83}, {"input": "missing usage", "twice": True})
        await self.rt.tasks.result(task.id, timeout=15)
        used = await self.rt.budgets.get(budget["id"])
        self.assertEqual([item["body"]["max_tokens"] for item in self.captured], [80, 3])
        self.assertEqual(used["usage"]["output_tokens"], 83)
        self.assertGreater(used["usage"]["input_tokens"], 2)
        self.assertEqual(used["estimated_model_calls"], 1)
        entries = (await self.rt.budgets.entries(budget["id"]))["items"]
        self.assertEqual([entry["estimated"] for entry in entries], [1, 0])

    async def test_explicit_amendment_keeps_usage_deadline_and_retry_lineage(self):
        session, budget, _, task = await self.create({"max_model_calls": 1})
        await self.failure(task.id, "BUDGET_EXHAUSTED")
        before = await self.rt.budgets.get(budget["id"])
        policy = {**before["policy"], "max_model_calls": 3}
        reason = "Operator approved two additional model calls"
        receipt = await self.rt.budgets.amend(budget["id"], policy, before["policy_hash"], "extension", reason)
        self.assertFalse(receipt["reused"])
        after = await self.rt.budgets.get(budget["id"])
        self.assertEqual(after["usage"], before["usage"])
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after["deadline_at"], before["deadline_at"])
        self.assertEqual(after["policy_hash"], receipt["policy_hash"])
        replay = await self.rt.budgets.amend(budget["id"], policy, before["policy_hash"], "extension", reason)
        self.assertTrue(replay["reused"])
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.budgets.amend(budget["id"], policy, before["policy_hash"], "extension", "Different reason")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(RuntimeFault) as stale:
            await self.rt.budgets.amend(budget["id"], policy, before["policy_hash"], "stale", reason)
        self.assertEqual(stale.exception.code, "REVISION_CONFLICT")
        with self.assertRaises(RuntimeFault) as below_usage:
            await self.rt.budgets.amend(budget["id"], {**policy, "max_input_tokens": 1}, after["policy_hash"], "too-small", reason)
        self.assertEqual(below_usage.exception.code, "INVALID_REQUEST")
        retry = await self.rt.tasks.retry(task.id, "approved-retry")
        await self.rt.tasks.result(retry.id, timeout=15)
        self.assertEqual((await self.rt.budgets.for_task(retry.id))["id"], budget["id"])
        self.assertEqual((await self.rt.budgets.get(budget["id"]))["usage"]["model_calls"], 3)
        self.assertEqual(len(self.captured), 3)
        self.assertEqual(await self.rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM budget_amendments").n), 1)
        self.assertEqual((await self.rt.budgets.create(session.id, {"max_model_calls": 1}, "limits"))["id"], budget["id"])

    async def test_native_source_rounds_use_persistent_model_limits(self):
        await self.rt.mcp.configure({"id": "fixture", "name": "Fixture", "command": sys.executable,
                                     "args": ["-B", str(Path(__file__).parent / "fixtures/mcp_server.py")]})
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False,
            mcp_bindings=[{"server_id": "fixture", "tools": ["echo"]}]))
        session = await self.rt.sessions.create()
        for calls in (16, 12):
            budget = await self.rt.budgets.create(session.id, {"max_model_calls": calls,
                "progress_tools": ["mcp:fixture:echo"]}, str(calls))
            thread = await self.rt.threads.create(session.id, "model-worker")
            task = await self.rt.tasks.submit(TaskSpec(session_id=session.id, agent_id="model-worker", thread_id=thread.id,
                payload={"input": "read steps: 14"}, idempotency_key=str(calls)), budget_id=budget["id"])
            if calls == 16:
                result = await self.rt.tasks.result(task.id, timeout=20)
                self.assertEqual(result.output["text"], "Source reading complete")
                self.assertEqual((await self.rt.budgets.get(budget["id"]))["usage"]["model_calls"], 15)
            else:
                await self.failure(task.id, "BUDGET_EXHAUSTED")
                self.assertEqual((await self.rt.budgets.get(budget["id"]))["usage"]["model_calls"], 12)

    async def test_stall_is_not_success_when_process_catches_the_error(self):
        _, _, _, task = await self.create({"max_stalled_model_calls": 1}, {"input": "test", "twice": True, "capture_error": True})
        failed = await self.failure(task.id, "PROGRESS_STALLED")
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(failed.result.output["text"], "first-model:v1")

    async def test_verified_artifact_allows_next_model_step(self):
        _, budget, _, task = await self.create({"max_stalled_model_calls": 1},
                                             {"input": "test", "publish_empty": True, "twice": True})
        result = await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(len(result.artifact_ids), 1)
        self.assertEqual(len(self.captured), 2)
        self.assertEqual((await self.rt.budgets.get(budget["id"]))["usage"]["tool_calls"], 1)
        artifact = await self.rt.artifacts.get(result.artifact_ids[0])
        def repeated(db):
            db.execute("UPDATE task_budgets SET stalled_calls=1 WHERE task_id=?", (task.id,))
            self.rt.budgets.progress(db, task.id, "publish_artifact", {"name": "report.txt", "sha256": artifact.sha256})
            return one(db, "SELECT stalled_calls FROM task_budgets WHERE task_id=?", (task.id,)).stalled_calls
        self.assertEqual(await self.rt.store.call(repeated, transaction=True), 1)

    async def test_scope_validation_and_old_hash_are_preserved(self):
        session, budget, _, task = await self.create(payload={"input": "test"})
        await self.rt.tasks.result(task.id, timeout=15)
        other = await self.rt.sessions.create()
        thread = await self.rt.threads.create(other.id, "model-worker")
        spec = TaskSpec(session_id=other.id, thread_id=thread.id, agent_id="model-worker", payload={"input": "test"}, idempotency_key="old")
        with self.assertRaises(RuntimeFault) as scope:
            await self.rt.tasks.submit(spec, budget_id=budget["id"])
        self.assertEqual(scope.exception.code, "BUDGET_SCOPE_MISMATCH")
        accepted = await self.rt.tasks.submit(spec)
        await self.rt.tasks.result(accepted.id, timeout=15)
        self.assertEqual((await self.rt.tasks.get(accepted.id)).request_hash, request_hash(spec))
        self.assertTrue((await self.rt.tasks.submit(spec)).reused)
        same = await self.rt.budgets.create(session.id, BudgetPolicy(), "limits")
        self.assertEqual(same["id"], budget["id"])

    async def test_wall_time_is_cumulative_and_stops_active_work(self):
        _, budget, _, task = await self.create({"max_elapsed_seconds": 2}, {"input": "hold after text"}, native=True)
        failed = await self.failure(task.id, "BUDGET_EXHAUSTED")
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(failed.result.output["text"], "first-model:v1")
        self.assertIsNotNone((await self.rt.budgets.get(budget["id"]))["deadline_at"])
        retry = await self.rt.tasks.retry(task.id, "time-retry")
        await self.failure(retry.id, "BUDGET_EXHAUSTED")
        self.assertEqual(len(self.captured), 1)

    async def test_map_members_and_repair_share_one_budget(self):
        await self.rt.agents.configure(self.config())
        session = await self.rt.sessions.create()
        budget = await self.rt.budgets.create(session.id, {"max_model_calls": 2}, "shared")
        graph = (WorkflowBuilder().node("start", "start").node("map", "map", agent_ids=["model-worker"], result_mode="all_settled",
                 payload={"input": "{{item}}"}).node("end", "end", outputs={"items": "{{nodes.map.results}}"})
                 .connect("start", "map").connect("map", "end").build())
        workflow = await self.rt.workflows.create("default", "Budget ownership", "flow", graph=graph)
        run = await self.rt.workflows.run(workflow.id, {"items": ["test", "test", "test"]}, "run", session_id=session.id, budget_id=budget["id"])
        async with asyncio.timeout(15):
            while (saved := await self.rt.workflows.get_run(run.id)).ended_at is None:
                await asyncio.sleep(.02)
        members = next(node for node in saved.nodes if node.node_id == "map").instances
        self.assertEqual(sorted(member["state"] for member in members), ["FAILED", "SUCCEEDED", "SUCCEEDED"])
        self.assertEqual({(await self.rt.budgets.for_task(member["task_id"]))["id"] for member in members}, {budget["id"]})
        repair = await self.rt.workflows.repair(run.id, "repair")
        async with asyncio.timeout(15):
            while (fixed := await self.rt.workflows.get_run(repair.id)).ended_at is None:
                await asyncio.sleep(.02)
        self.assertEqual(len(self.captured), 2)
        self.assertEqual((await self.rt.budgets.get(budget["id"]))["usage"]["model_calls"], 2)
        newer = next(node for node in fixed.nodes if node.node_id == "map").instances
        self.assertEqual({(await self.rt.budgets.for_task(member["task_id"]))["id"] for member in newer}, {budget["id"]})


if __name__ == "__main__":
    unittest.main()
