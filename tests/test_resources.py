"""Resource capacity, transactional leases and actual occupied phase boundaries."""

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from oea import Runtime, RuntimeConfig, RuntimeFault, QuantityRequirement, ResourceRequirement
from ma_runtime.resources import ResourceCoordinator
from oea import AgentDescriptor, AdapterEvent, TaskResult, ProbeResult
from ma_runtime.tools import ToolSpec
from ma_runtime.models import ResourceLeaseSpec


class HeldExecution:
    def __init__(self):
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.running = set()

    def describe(self):
        return AgentDescriptor(agent_id="worker", cancellation="unsupported")

    async def execute(self, request, context):
        self.running.add(request.run_id)
        try:
            self.started.put_nowait(request.run_id)
            await self.release.wait()
            if request.payload.get("unresolved"):
                await context.invoke_tool("unresolved_remote", {}, "remote-action")
            yield AdapterEvent(type="completed", result=TaskResult(output={"text": "Completed owned execution"}))
        finally:
            self.running.discard(request.run_id)

    async def probe(self, reference):
        active = reference["run_id"] in self.running
        return ProbeResult(state="RUNNING" if active else "STOPPED", confirmed_stopped=not active)

    async def aclose(self):
        self.release.set()


class UnresolvedRemoteTool:
    def describe(self):
        return ToolSpec(name="unresolved_remote", side_effects="EXTERNAL", resources=(ResourceRequirement(key="remote"),))

    async def invoke(self, request):
        raise ConnectionError("No terminal remote response")


class UnclaimedRemoteTool(UnresolvedRemoteTool):
    def describe(self):
        return ToolSpec(name="unresolved_remote", side_effects="EXTERNAL")


class StoppedExecution:
    def describe(self):
        return AgentDescriptor(agent_id="worker", max_concurrency=1)

    async def execute(self, request, context):
        if request.payload.get("unresolved"):
            await context.retain_partial_result(TaskResult(output={"text": "Retained phase evidence"}))
            await context.invoke_tool("unresolved_remote", {}, request.task_id + ":original")
        yield AdapterEvent(type="completed", result=TaskResult(output={"text": "Next local execution"}))

    async def probe(self, reference):
        return ProbeResult(state="STOPPED", confirmed_stopped=True, evidence="Execution coroutine exited")

    async def aclose(self):
        pass


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_unknown_leases_still_count_toward_admission_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp, config=RuntimeConfig(max_queued_tasks=1)) as rt:
                for key in ("first", "second"):
                    await rt.resources.configure({"key": key, "limit": 1})
                spec = {"owner": "external", "scope": "scope", "phase": "call", "idempotency_key": "first",
                        "quantities": [{"key": "first"}], "ttl_seconds": .02}
                with patch("ma_runtime.resources.now_ms", return_value=1_000_000) as clock:
                    lease = await rt.resources.request(spec)
                    await rt.resources.tick()
                    granted = await rt.resources.get(lease["id"])
                    self.assertEqual(granted["state"], "GRANTED")
                    # Expiry follows the lease clock, independently of event-loop timer resolution.
                    clock.return_value = granted["expires_at"] - 1
                    await rt.resources.tick()
                    self.assertEqual((await rt.resources.get(lease["id"]))["state"], "GRANTED")
                    clock.return_value = granted["expires_at"]
                    await rt.resources.tick()
                    self.assertEqual((await rt.resources.get(lease["id"]))["state"], "ISOLATED")
                    with self.assertRaises(RuntimeFault) as failure:
                        await rt.resources.request({**spec, "idempotency_key": "second", "quantities": [{"key": "second"}]})
                    self.assertEqual(failure.exception.code, "QUEUE_FULL")
                    await rt.resources.release_lease(lease["id"], confirmed_stopped=True, evidence="The synthetic owner has finished")
                    next_lease = await rt.resources.request({**spec, "idempotency_key": "second", "quantities": [{"key": "second"}]})
                    await rt.resources.tick()
                    self.assertEqual((await rt.resources.get(next_lease["id"]))["state"], "GRANTED")

    async def test_unknown_business_outcomes_without_resource_claims_do_not_accumulate_live_owners(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp, adapters={"worker": StoppedExecution()}) as rt:
                rt.tools.register(UnclaimedRemoteTool(), allowed_agent_ids=["worker"])
                session = await rt.sessions.create()
                for index in range(5):
                    thread = await rt.threads.create(session.id, "worker")
                    task = await rt.tasks.submit({"session_id": session.id, "thread_id": thread.id, "agent_id": "worker",
                        "idempotency_key": "unknown-" + str(index), "payload": {"unresolved": True}})
                    with self.assertRaises(RuntimeFault):
                        await rt.tasks.result(task.id, timeout=5)
                    self.assertEqual((await rt.tasks.get(task.id)).state, "UNCERTAIN")
                async with asyncio.timeout(5):
                    while rt.active:
                        await asyncio.sleep(.01)
                self.assertFalse(rt.resources.owner_keys)
                self.assertFalse(rt.resources.isolated)
                self.assertFalse(rt.uncertain_slots)
                status = await rt.resources.status({"session_id": session.id})
                self.assertFalse(status["items"])
                self.assertEqual(status["workload"]["executing_instances"], {})
    async def test_owner_exit_during_lease_receipt_delivery_does_not_orphan_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = HeldExecution()
            async with Runtime.open(temp, adapters={"worker": worker}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                task = await rt.threads.turn(thread.id, "Hold", "receipt-race")
                run_id = await asyncio.wait_for(worker.started.get(), 5)
                await rt.resources.configure({"key": "service", "limit": 1})
                committed, deliver = asyncio.Event(), asyncio.Event()
                cleanup_committed, finish_cleanup, cleanup_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                original_call = rt.store.call
                original_release = rt.resources.release_run_leases
                async def delayed_delivery(fn, **options):
                    result = await original_call(fn, **options)
                    if asyncio.current_task().get_name() == "bound-lease-submit" and fn.__name__ == "insert":
                        committed.set()
                        await deliver.wait()
                    elif fn.__name__ == "stop" and result:
                        cleanup_committed.set()
                        await finish_cleanup.wait()
                    return result
                async def observed_release(*args, **kwargs):
                    await original_release(*args, **kwargs)
                    cleanup_finished.set()
                with (
                    patch.object(rt.store, "call", new=delayed_delivery),
                    patch.object(rt.resources, "release_run_leases", new=observed_release),
                ):
                    submission = asyncio.create_task(rt.resources.request({"owner": run_id, "owner_run_id": run_id,
                        "scope": session.id, "phase": "local", "idempotency_key": "racing", "quantities": [{"key": "service"}]}),
                        name="bound-lease-submit")
                    try:
                        await asyncio.wait_for(committed.wait(), 5)
                        worker.release.set()
                        await rt.tasks.result(task.id, timeout=5)
                        deliver.set()
                        await asyncio.wait_for(cleanup_committed.wait(), 5)
                        receipt = await asyncio.wait_for(submission, 5)
                        # A durable RELEASED row can be visible before local cleanup returns.
                        self.assertEqual((await rt.resources.get(receipt["id"]))["state"], "RELEASED")
                        self.assertIn(receipt["id"], rt.resources.pending)
                        finish_cleanup.set()
                        await asyncio.wait_for(cleanup_finished.wait(), 5)
                        self.assertFalse(rt.resources.pending)
                        self.assertFalse(rt.resources.run_leases)
                        self.assertNotIn("service", rt.resources.quantities)
                    finally:
                        deliver.set()
                        finish_cleanup.set()
                        worker.release.set()

    async def test_run_bound_lease_recovers_after_stop_receipt_precedes_lease_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = HeldExecution()
            rt = await Runtime.open(temp, adapters={"worker": worker}).__aenter__()
            with patch.object(rt.resources, "release_run_leases", new=AsyncMock()):
                try:
                    rt.tools.register(UnresolvedRemoteTool(), allowed_agent_ids=["worker"])
                    session = await rt.sessions.create()
                    thread = await rt.threads.create(session.id, "worker")
                    task = await rt.tasks.submit({"session_id": session.id, "thread_id": thread.id, "agent_id": "worker",
                                                  "idempotency_key": "interrupted-cleanup", "payload": {"unresolved": True}})
                    run_id = await asyncio.wait_for(worker.started.get(), 5)
                    await rt.resources.configure({"key": "service", "limit": 1})
                    lease = await rt.resources.request({"owner": run_id, "owner_run_id": run_id,
                        "scope": session.id, "phase": "local", "idempotency_key": "bound", "quantities": [{"key": "service"}]})
                    await rt.resources.tick()
                    worker.release.set()
                    with self.assertRaises(RuntimeFault):
                        await rt.tasks.result(task.id, timeout=5)
                    async with asyncio.timeout(5):
                        while rt.resources.quantities.get("execution:global"):
                            await asyncio.sleep(.01)
                    self.assertEqual((await rt.resources.get(lease["id"]))["state"], "GRANTED")
                finally:
                    await rt.aclose()
            async with Runtime.open(temp, adapters={"worker": HeldExecution()}) as recovered:
                self.assertEqual((await recovered.resources.get(lease["id"]))["state"], "RELEASED")
                self.assertFalse(recovered.resources.run_leases)
                self.assertFalse(recovered.uncertain_slots)
                self.assertTrue(recovered.resources.holders["remote"])
                self.assertEqual((await recovered.tasks.get(task.id)).state, "UNCERTAIN")

    async def test_run_bound_lease_requires_modern_view_without_changing_legacy_requests(self):
        from ma_runtime.bridges.dispatch import dispatch
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.resources.configure({"key": "service", "limit": 1})
                spec = {"owner": "legacy-owner", "scope": "scope", "phase": "call", "idempotency_key": "legacy",
                        "quantities": [{"key": "service"}]}
                legacy = await dispatch(rt, "resources.request", {"spec": spec}, view_version="1.0")
                self.assertNotIn("owner_run_id", legacy["spec"])
                with self.assertRaises(RuntimeFault) as failure:
                    await dispatch(rt, "resources.request", {"spec": {**spec, "owner_run_id": "legacy-owner"}}, view_version="1.0")
                self.assertEqual(failure.exception.code, "VIEW_VERSION_REQUIRED")
                self.assertEqual((await rt.resources.get(legacy["id"]))["spec"], legacy["spec"])

    async def test_explicit_mcp_recovery_window_requires_modern_view(self):
        import sys
        from ma_runtime.bridges.dispatch import dispatch
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.mcp.configure({"id": "fixture", "name": "Fixture", "command": sys.executable})
                legacy = await dispatch(rt, "mcp.policy", {"server_id": "fixture"}, view_version="1.0")
                self.assertNotIn("recovery_timeout_seconds", legacy["policy"])
                with self.assertRaises(RuntimeFault) as failure:
                    await dispatch(rt, "mcp.configure_policy", {"server_id": "fixture", "policy": {"recovery_timeout_seconds": 0}}, view_version="1.0")
                self.assertEqual(failure.exception.code, "VIEW_VERSION_REQUIRED")
                self.assertEqual(rt.mcp.capacity.policy("fixture").recovery_timeout_seconds, 30)
                await rt.mcp.configure_policy("fixture", {"recovery_timeout_seconds": 0})
                with self.assertRaises(RuntimeFault) as failure:
                    await dispatch(rt, "mcp.policy", {"server_id": "fixture"}, view_version="1.0")
                self.assertEqual(failure.exception.code, "VIEW_VERSION_REQUIRED")

    async def test_run_bound_expired_lease_releases_on_actual_owner_exit_and_wakes_queued_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = HeldExecution()
            async with Runtime.open(temp, adapters={"worker": worker}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                task = await rt.threads.turn(thread.id, "Hold an execution", "owned")
                run_id = await asyncio.wait_for(worker.started.get(), 5)
                await rt.resources.configure({"key": "service", "limit": 1})
                spec = {"owner": run_id, "owner_run_id": run_id, "scope": session.id, "phase": "local",
                        "idempotency_key": "owned-lease", "quantities": [{"key": "service"}], "ttl_seconds": .02}
                owned = await rt.resources.request(spec)
                await rt.resources.tick()
                next_lease = await rt.resources.request({"owner": "next", "scope": session.id, "phase": "local",
                    "idempotency_key": "next", "quantities": [{"key": "service"}]})
                await asyncio.sleep(.03)
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(owned["id"]))["state"], "ISOLATED")
                self.assertEqual((await rt.resources.get(next_lease["id"]))["state"], "QUEUED")
                status = await rt.resources.status({"session_id": session.id})
                self.assertTrue(any(item["owner_id"] == owned["id"] for item in status["items"]))
                self.assertEqual(status["workload"]["executing_instances"], {"worker": 1})
                worker.release.set()
                await rt.tasks.result(task.id, timeout=5)
                async with asyncio.timeout(5):
                    while (await rt.resources.get(next_lease["id"]))["state"] != "GRANTED":
                        await asyncio.sleep(.01)
                self.assertEqual((await rt.resources.get(owned["id"]))["state"], "RELEASED")
                self.assertFalse(rt.resources.run_leases)
                replay = await rt.resources.request(spec)
                self.assertTrue(replay["reused"])
                self.assertEqual(replay["state"], "RELEASED")
                self.assertNotIn("owner_run_id", ResourceLeaseSpec(owner="manual", scope="scope", phase="local",
                    idempotency_key="legacy", quantities=(QuantityRequirement(key="service"),)).model_dump())

    async def test_stopped_local_execution_releases_slot_but_preserves_remote_claim_and_partial_result(self):
        with tempfile.TemporaryDirectory() as temp:
            config = RuntimeConfig(max_concurrency=2, reserved_external_slots=1)
            async with Runtime.open(temp, config=config, adapters={"worker": StoppedExecution()}) as rt:
                rt.tools.register(UnresolvedRemoteTool(), allowed_agent_ids=["worker"])
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                task = await rt.tasks.submit({"session_id": session.id, "thread_id": thread.id, "agent_id": "worker",
                                              "idempotency_key": "first", "payload": {"unresolved": True}})
                with self.assertRaises(RuntimeFault):
                    await rt.tasks.result(task.id, timeout=5)
                snapshot = await rt.tasks.get(task.id)
                self.assertEqual(snapshot.state, "UNCERTAIN")
                self.assertEqual(snapshot.result.output["text"], "Retained phase evidence")
                self.assertFalse(rt.uncertain_slots)
                self.assertNotIn("execution:global", rt.resources.quantities)
                self.assertTrue(rt.resources.holders["remote"])
                next_thread = await rt.threads.create(session.id, "worker")
                following = await rt.threads.turn(next_thread.id, "Read", "next")
                result = await rt.tasks.result(following.id, timeout=5)
                self.assertEqual(result.output["text"], "Next local execution")
            async with Runtime.open(temp, config=config, adapters={"worker": StoppedExecution()}) as rt:
                self.assertFalse(rt.uncertain_slots)
                self.assertNotIn("execution:global", rt.resources.quantities)
                self.assertTrue(rt.resources.holders["remote"])
                self.assertEqual((await rt.tasks.get(task.id)).state, "UNCERTAIN")

    async def test_releasing_one_owner_preserves_other_generations_and_reverse_index(self):
        resources = ResourceCoordinator()
        resources.define("connections", 2)
        claim = (QuantityRequirement(key="connections"),)
        self.assertTrue(resources.acquire("consumer:generation-1", (), claim))
        self.assertTrue(resources.acquire("consumer:generation-2", (), claim))
        resources.isolate("consumer:generation-1", "Awaiting original execution")
        resources.resolve("consumer:generation-1")
        resources.resolve("consumer:generation-1")
        self.assertEqual(resources.quantities["connections"], {"consumer:generation-2": 1})
        self.assertNotIn("consumer:generation-1", resources.owner_keys)
        resources.release("consumer:generation-2")
        self.assertFalse(resources.owner_keys)
        self.assertFalse(resources.quantities)

    async def test_atomic_quantities_and_phase_cancellation(self):
        resources = ResourceCoordinator()
        resources.define("a", 2)
        resources.define("b", 1)
        q = QuantityRequirement
        self.assertTrue(resources.acquire("first", (), (q(key="a"), q(key="b"))))
        self.assertFalse(resources.acquire("second", (ResourceRequirement(key="file"),), (q(key="a"), q(key="b"))))
        self.assertNotIn("file", resources.holders)
        self.assertEqual(resources.quantities["a"], {"first": 1})
        self.assertTrue(resources.acquire("first", (), (q(key="a"), q(key="b"))))
        resources.isolate("first", "Unknown operation")
        resources.release("first")
        self.assertFalse(resources.can_acquire("second", (), (q(key="b"),)))
        entered = asyncio.Event()
        async def blocked():
            async with resources.phase("waiting", (q(key="b"),), timeout=2):
                entered.set()
        waiter = asyncio.create_task(blocked())
        await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(resources.phase_waiters)
        resources.resolve("first")
        await blocked()
        self.assertTrue(entered.is_set())
        self.assertFalse(resources.quantities)

    async def test_lease_queue_expiry_and_recovery_do_not_free_unknown_occupancy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = RuntimeConfig(max_concurrency=2, reserved_external_slots=0)
            async with Runtime.open(root, config=config) as rt:
                await rt.resources.configure({"key": "service", "unit": "request", "limit": 1})
                original = await rt.resources.inspect()
                self.assertTrue((await rt.resources.preflight([{"key": "service"}]))["admissible"])
                self.assertEqual(original, await rt.resources.inspect())
                spec = {"owner": "owner", "scope": "scope", "phase": "call", "idempotency_key": "one",
                        "quantities": [{"key": "service"}], "ttl_seconds": .04}
                first = await rt.resources.request(spec)
                again = await rt.resources.request(spec)
                self.assertEqual(first["id"], again["id"])
                self.assertTrue(again["reused"])
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(first["id"]))["state"], "GRANTED")
                second = await rt.resources.request({**spec, "idempotency_key": "two", "ttl_seconds": 10})
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(second["id"]))["state"], "QUEUED")
                with self.assertRaises(RuntimeFault) as conflict:
                    await rt.resources.request({**spec, "phase": "changed"})
                self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
                await asyncio.sleep(.05)
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(first["id"]))["state"], "ISOLATED")
                with self.assertRaises(RuntimeFault):
                    await rt.resources.release_lease(first["id"])
            async with Runtime.open(root, config=config) as rt:
                self.assertEqual((await rt.resources.get(first["id"]))["state"], "ISOLATED")
                self.assertFalse((await rt.resources.preflight([{"key": "service"}]))["admissible"])
                await rt.resources.release_lease(first["id"], confirmed_stopped=True, evidence="Owner stopped")
                await rt.resources.release_lease(first["id"])
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(second["id"]))["state"], "GRANTED")
            async with Runtime.open(root, config=config) as rt:
                self.assertEqual((await rt.resources.get(second["id"]))["state"], "ISOLATED")
