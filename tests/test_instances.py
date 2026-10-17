"""Managed fan-out contracts with temporary synthetic state and no model access."""

import asyncio
import tempfile
import sys
import unittest
from pathlib import Path

import httpx

from oea import (AdapterEvent, AgentConfig, AgentDescriptor, CancelReceipt, CheckpointProposal, InstanceBatchSpec, ProbeResult,
                 PlatformClient, Runtime, RuntimeConfig, RuntimeFault, TaskResult, TaskSpec,
                 WaitCondition, WorkflowBuilder)
from ma_runtime.bridges.compact import CompactBridge
from ma_runtime.bridges.dispatch import dispatch
from ma_runtime.errors import AdapterFailure
from ma_runtime.models import json_dump
from ma_runtime.store import one
from ma_runtime.web.app import create_app


class Worker:
    def __init__(self, identity="worker", capacity=64):
        self.identity, self.capacity = identity, capacity
        self.active = self.peak = 0
        self.seen = []
        self.running = {}

    def describe(self):
        return AgentDescriptor(agent_id=self.identity, max_concurrency=self.capacity)

    async def execute(self, request, context):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.seen.append(request.payload)
        self.running[request.run_id] = context.cancelled
        try:
            await asyncio.sleep(request.payload.get("delay", 0.04))
            if context.cancelled.is_set():
                raise asyncio.CancelledError
            if request.payload.get("fail"):
                raise AdapterFailure("Synthetic failure")
            yield AdapterEvent(type="completed", result=TaskResult(output=request.payload))
        finally:
            self.active -= 1
            self.running.pop(request.run_id, None)

    async def cancel(self, ref):
        event = self.running.get(ref["run_id"])
        if event:
            event.set()
        return CancelReceipt(accepted=True, confirmed_stopped=event is None)

    async def probe(self, ref):
        running = ref["run_id"] in self.running
        return ProbeResult(state="RUNNING" if running else "STOPPED", confirmed_stopped=not running,
                           evidence="Synthetic worker lifecycle")

    async def aclose(self):
        pass


def map_graph(agent_ids=("worker",), **config):
    return (WorkflowBuilder().node("start", "start")
            .node("fanout", "map", agent_ids=agent_ids,
                  payload={"value": "{{item}}", "index": "{{index}}"}, **config)
            .node("end", "end", outputs={"items": "{{nodes.fanout.outputs}}"})
            .connect("start", "fanout").connect("fanout", "end").build())


class InstanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-instances-")
        self.root = Path(self.temp.name)
        self.worker = Worker()
        self.rt = await Runtime.open(self.root / "state", RuntimeConfig(max_concurrency=8, reserved_external_slots=0),
                                     adapters={"worker": self.worker}).__aenter__()
        self.session = await self.rt.sessions.create()

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    def spec(self, key="batch", count=12, **options):
        return InstanceBatchSpec(session_id=self.session.id, agent_ids=("worker",),
            payloads=tuple({"index": number} for number in range(count)), idempotency_key=key, **options)

    async def settled(self, batch_id, states=("SUCCEEDED", "FAILED", "CANCELLED")):
        for _ in range(200):
            batch = await self.rt.instances.get(batch_id, include_outputs=True)
            if batch["state"] in states:
                return batch
            await asyncio.sleep(0.01)
        self.fail("Batch did not settle")

    async def test_independent_instances_order_budget_and_idempotency(self):
        spec = self.spec(max_concurrency=3)
        first, second = await asyncio.gather(self.rt.instances.spawn(spec), self.rt.instances.spawn(spec))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len({item["thread_id"] for item in first["instances"]}), 12)
        result = await self.rt.instances.result(first["id"], timeout=3)
        self.assertEqual([m["result"]["output"]["index"] for m in result["instances"]], list(range(12)))
        self.assertEqual(len(self.worker.seen), 12)
        self.assertEqual(self.worker.peak, 3)
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.instances.spawn(self.spec(count=2))
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_global_budget_and_least_loaded_pool(self):
        second = Worker("second")
        self.rt.adapters["second"] = second
        batch = await self.rt.instances.spawn(self.spec(count=24, max_concurrency=32).model_copy(
            update={"agent_ids": ("worker", "second"), "routing": "least_loaded"}))
        self.assertEqual([m["agent_id"] for m in batch["instances"]], ["worker", "second"] * 12)
        await self.rt.instances.result(batch["id"], timeout=4)
        self.assertLessEqual(self.worker.peak + second.peak, 8)
        self.assertGreater(self.worker.peak + second.peak, 3)

    async def test_atomic_rollback_for_invalid_dependency_and_queue_capacity(self):
        with self.assertRaises(RuntimeFault):
            await self.rt.instances.spawn(self.spec(depends_on=("missing-task",)))
        counts = await self.rt.store.call(lambda db: (one(db, "SELECT COUNT(*) AS n FROM instance_batches").n,
                                                      one(db, "SELECT COUNT(*) AS n FROM threads").n))
        self.assertEqual(counts, (0, 0))
        self.rt.config.max_queued_tasks = 2
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.instances.spawn(self.spec())
        self.assertEqual(error.exception.code, "QUEUE_FULL")

    async def test_cross_project_agents_and_external_endpoints_are_rejected(self):
        folder = self.root / "project"
        folder.mkdir()
        project = await self.rt.projects.create("Other", str(folder), "other")
        await self.rt.agents.configure(AgentConfig(id="foreign", name="Foreign", project_id=project.id, kind="mock"))
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.instances.spawn(self.spec().model_copy(update={"agent_ids": ("foreign",)}))
        self.assertEqual(error.exception.code, "PROJECT_MISMATCH")
        from agent.external import ExternalAgent
        self.rt.adapters["external"] = ExternalAgent("external")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.instances.spawn(self.spec().model_copy(update={"agent_ids": ("external",)}))
        self.assertEqual(error.exception.code, "UNSUPPORTED_CAPABILITY")

    async def test_configured_agent_budget_and_available_pool_routing(self):
        for limit in (1, 4):
            await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock", max_concurrency=limit))
            worker = Worker()
            self.rt.adapters["worker"] = worker
            batch = await self.rt.instances.spawn(self.spec(f"limit-{limit}", count=8, max_concurrency=8))
            await self.rt.instances.result(batch["id"], timeout=3)
            self.assertEqual(worker.peak, limit)
        await self.rt.agents.configure(AgentConfig(id="unconfigured", name="Unconfigured", kind="llm"))
        batch = await self.rt.instances.spawn(self.spec("available", count=4).model_copy(
            update={"agent_ids": ("unconfigured", "worker"), "routing": "least_loaded"}))
        self.assertEqual({m["agent_id"] for m in batch["instances"]}, {"worker"})
        await self.rt.instances.result(batch["id"], timeout=3)

    async def test_cancel_waits_and_does_not_dispatch_queued_members(self):
        spec = self.spec(count=20, max_concurrency=2).model_copy(update={"payloads": tuple({"delay": 0.4} for _ in range(20))})
        batch = await self.rt.instances.spawn(spec)
        await asyncio.sleep(0.05)
        await self.rt.instances.cancel(batch["id"])
        final = await self.settled(batch["id"])
        self.assertEqual(final["state"], "CANCELLED")
        self.assertLessEqual(len(self.worker.seen), 2)
        self.assertTrue((await self.rt.instances.cancel(batch["id"]))["confirmed_stopped"])

    async def test_failed_batch_preserves_member_evidence(self):
        spec = self.spec(count=3).model_copy(update={"payloads": ({"index": 0}, {"fail": True}, {"index": 2})})
        batch = await self.rt.instances.spawn(spec)
        final = await self.settled(batch["id"])
        self.assertEqual(final["counts"], {"SUCCEEDED": 2, "FAILED": 1})
        self.assertIsNotNone(final["instances"][1]["error"])
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.instances.result(batch["id"])
        self.assertEqual(error.exception.code, "BATCH_FAILED")

    async def test_map_dynamic_fanout_order_empty_input_and_reused_run(self):
        workflow = await self.rt.workflows.create("default", "Map", "map", graph=map_graph(max_concurrency=3))
        run = await self.rt.workflows.run(workflow.id, {"items": list(range(17))}, "run")
        replay = await self.rt.workflows.run(workflow.id, {"items": list(range(17))}, "run")
        self.assertEqual(run.id, replay.id)
        result = await self.rt.workflows.result(run.id, timeout=4)
        self.assertEqual([item["value"] for item in result["outputs"]["items"]], list(range(17)))
        node = next(n for n in (await self.rt.workflows.get_run(run.id)).nodes if n.node_id == "fanout")
        self.assertIsNotNone(node.batch_id)
        self.assertEqual(len(node.instances), 17)
        self.assertEqual(self.worker.peak, 3)
        empty = await self.rt.workflows.run(workflow.id, {"items": []}, "empty")
        self.assertEqual((await self.rt.workflows.result(empty.id, timeout=2))["outputs"], {"items": []})

    async def test_map_validation_and_runtime_item_bound(self):
        invalid = map_graph().model_dump(mode="json")
        invalid["nodes"][1]["config"]["items"] = "{{item}}"
        self.assertFalse((await self.rt.workflows.validate("default", invalid))["valid"])
        workflow = await self.rt.workflows.create("default", "Map", "map", graph=map_graph())
        run = await self.rt.workflows.run(workflow.id, {"items": list(range(129))}, "oversized")
        with self.assertRaises(RuntimeFault):
            await self.rt.workflows.result(run.id, timeout=2)
        self.assertEqual(len(self.worker.seen), 0)

    async def test_http_sdk_and_compact_discovery_spawn_and_results(self):
        import json
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                batch = await client.instances.spawn(self.spec(count=4))
                result = await client.instances.result(batch["id"], timeout=2)
                self.assertEqual(result["state"], "SUCCEEDED")
                self.assertEqual(len((await client.instances.list(self.session.id))["items"]), 1)
        async def invoke(method, params):
            return json.loads(json_dump(await dispatch(self.rt, method, params)))
        bridge = CompactBridge(invoke)
        self.assertEqual(set(bridge.guide("instances", "spawn")["schemas"]), {"InstanceBatchSpec"})
        response = await bridge.call("instances", "spawn", {"spec": self.spec("mcp", 2).model_dump(mode="json")})
        self.assertEqual(len(response["result"]["instances"]), 2)
        with self.assertRaises(RuntimeFault):
            await bridge.call("instances", "result", {"batch_id": batch["id"], "timeout": 31})

    async def test_map_recovery_preserves_batch_members_and_does_not_replay(self):
        await self.rt.agents.configure(AgentConfig(id="saved", name="Saved", kind="mock"))
        self.rt.scheduler.cancel()
        await self.rt.scheduler
        workflow = await self.rt.workflows.create("default", "Durable map", "map", graph=map_graph(("saved",)))
        run = await self.rt.workflows.run(workflow.id, {"items": [1, 2, 3]}, "run")
        await self.rt.workflows.tick()
        before = next(n for n in (await self.rt.workflows.get_run(run.id)).nodes if n.node_id == "fanout")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state", RuntimeConfig(reserved_external_slots=0)).__aenter__()
        await self.rt.workflows.result(run.id, timeout=4)
        after = next(n for n in (await self.rt.workflows.get_run(run.id)).nodes if n.node_id == "fanout")
        self.assertEqual(before.batch_id, after.batch_id)
        self.assertEqual([m["task_id"] for m in before.instances], [m["task_id"] for m in after.instances])
        self.assertEqual(len((await self.rt.tasks.list(run.session_id)).items), 3)

    async def test_map_wait_resume_and_cancel(self):
        await self.rt.agents.configure(AgentConfig(id="waiting", name="Waiting", kind="mock"))
        graph = map_graph(("waiting",)).model_dump(mode="json")
        graph["nodes"][1]["config"]["payload"] = {"input": "{{item}}", "_mock": {"wait_for_input": True, "delay": 0}}
        workflow = await self.rt.workflows.create("default", "Waiting map", "map", graph=graph)
        run = await self.rt.workflows.run(workflow.id, {"items": ["first", "second"]}, "run")
        for _ in range(100):
            current = await self.rt.workflows.get_run(run.id)
            if current.state == "WAITING":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(current.state, "WAITING")
        node = next(n for n in current.nodes if n.node_id == "fanout")
        for member in node.instances:
            task = await self.rt.tasks.get(member["task_id"])
            await self.rt.workflows.resume_node(run.id, "fanout", {"wait_token": task.wait.wait_token, "input": "continue"},
                                                 f"resume-{member['ordinal']}", instance_index=member["ordinal"])
        await self.rt.workflows.result(run.id, timeout=3)
        cancelled = await self.rt.workflows.run(workflow.id, {"items": ["third", "fourth"]}, "cancel")
        await self.rt.workflows.cancel(cancelled.id)
        for _ in range(100):
            current = await self.rt.workflows.get_run(cancelled.id)
            if current.state == "CANCELLED":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(current.state, "CANCELLED")

    async def test_map_failure_stops_downstream_and_settles_other_members(self):
        graph = map_graph().model_dump(mode="json")
        graph["nodes"][1]["config"]["payload"] = {"fail": "{{item.fail}}", "delay": "{{item.delay}}"}
        workflow = await self.rt.workflows.create("default", "Failing map", "map", graph=graph)
        run = await self.rt.workflows.run(workflow.id, {"items": [{"fail": True, "delay": 0.01}, {"fail": False, "delay": 0.15}]}, "run")
        with self.assertRaises(RuntimeFault):
            await self.rt.workflows.result(run.id, timeout=3)
        final = await self.rt.workflows.get_run(run.id)
        self.assertEqual(final.state, "FAILED")
        self.assertEqual(next(n for n in final.nodes if n.node_id == "end").state, "SKIPPED")
        self.assertTrue(all(t.state in {"FAILED", "CANCELLED", "SUCCEEDED"} for t in (await self.rt.tasks.list(run.session_id)).items))

    async def test_uncertain_member_blocks_agent_capacity_and_downstream(self):
        class UnknownWorker(Worker):
            def describe(self):
                return AgentDescriptor(agent_id="unknown", max_concurrency=1, tool_mediation="partial")
        unknown = UnknownWorker("unknown", capacity=1)
        self.rt.adapters["unknown"] = unknown
        batch = await self.rt.instances.spawn(self.spec(count=2).model_copy(
            update={"agent_ids": ("unknown",), "payloads": ({"fail": True}, {"index": 1})}))
        final = await self.settled(batch["id"], states=("UNCERTAIN",))
        await asyncio.sleep(0.1)
        current = await self.rt.instances.get(batch["id"])
        self.assertEqual(len(self.rt.uncertain_slots), 1)
        self.assertEqual(current["instances"][1]["state"], "QUEUED")
        self.assertEqual(len(unknown.seen), 1)
        await self.rt.instances.cancel(batch["id"])
        self.assertFalse((await self.rt.instances.cancel(batch["id"]))["confirmed_stopped"])
        self.assertFalse(unknown.running)
        await self.rt.tasks.reconcile(final["instances"][0]["task_id"], {
            "resolution": "CANCELLED", "confirmed_stopped": True, "external_actions_confirmed": True,
            "evidence": "Synthetic worker stopped; this fixture has no external operations",
        })
        self.assertEqual(len(self.rt.uncertain_slots), 0)
        self.assertFalse(self.rt._uncertain_owners)
        self.assertEqual((await self.rt.instances.get(batch["id"]))["state"], "CANCELLED")

    async def test_process_sdk_can_spawn_scoped_children(self):
        script = self.root / "parent.py"
        script.write_text('''from oea import AdapterEvent, AgentDescriptor, TaskResult
from oea.stdio import run_agent
class Parent:
    def describe(self):
        return AgentDescriptor(agent_id="process", execution_mode="subprocess", cancellation="terminate_owned_process", supports_probe=False)
    async def execute(self, request, context):
        batch = await context.spawn_instances({"session_id":request.session_id,"agent_ids":["worker"],"payloads":[{"index":1},{"index":2}],"idempotency_key":"children"})
        yield AdapterEvent(type="completed", result=TaskResult(output=batch))
    async def aclose(self):
        pass
if __name__ == "__main__":
    run_agent(Parent())
''', encoding="utf-8")
        await self.rt.agents.configure(AgentConfig(id="process", name="Process parent", kind="process",
            command=(sys.executable, "-B", str(script))))
        thread = await self.rt.threads.create(self.session.id, "process")
        task = await self.rt.tasks.submit(TaskSpec(session_id=self.session.id, thread_id=thread.id,
            agent_id="process", payload={}, idempotency_key="process"))
        result = await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(result.output["parent_task_id"], task.id)
        self.assertEqual((await self.rt.instances.result(result.output["id"], timeout=3))["state"], "SUCCEEDED")


class Parent(Worker):
    def describe(self):
        return AgentDescriptor(agent_id=self.identity, max_concurrency=1, supports_suspend=True,
                               checkpoint_mode="opaque_json", nested_execution="runtime_mediated")

    async def execute(self, request, context):
        if request.resume_ref:
            if request.payload.get("fail_after_children"):
                raise AdapterFailure("Synthetic parent failure after children")
            yield AdapterEvent(type="completed", result=TaskResult(output={"children": request.resume_input["tasks"]}))
        else:
            batch = await context.spawn_instances({"session_id": request.session_id, "agent_ids": ["worker"],
                "payloads": [{"index": n, "delay": request.payload.get("delay", 0.01)} for n in range(5)],
                "idempotency_key": "children", "max_concurrency": 2})
            yield AdapterEvent(type="suspended", checkpoint=CheckpointProposal(ref={"spawned": True}),
                               wait=WaitCondition(kind="tasks", task_ids=tuple(m["task_id"] for m in batch["instances"])))


class ParentInstanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_cancel_reaches_queued_descendants_before_dispatch(self):
        class Root(Parent):
            async def execute(self, request, context):
                batch = await context.spawn_instances({"session_id": request.session_id, "agent_ids": ["middle"],
                    "payloads": [{"delay": 0.4}], "idempotency_key": "middle"})
                yield AdapterEvent(type="suspended", checkpoint=CheckpointProposal(ref={"spawned": True}),
                    wait=WaitCondition(kind="tasks", task_ids=tuple(m["task_id"] for m in batch["instances"])))
        with tempfile.TemporaryDirectory(prefix="oea-nested-cancel-") as directory:
            async with Runtime.open(directory, RuntimeConfig(max_concurrency=2, reserved_external_slots=0),
                                    adapters={"parent": Root("parent"), "middle": Parent("middle"), "worker": Worker()}) as runtime:
                session = await runtime.sessions.create()
                thread = await runtime.threads.create(session.id, "parent")
                task = await runtime.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="parent", payload={}, idempotency_key="root"))
                for _ in range(100):
                    tasks = (await runtime.tasks.list(session.id)).items
                    if len(tasks) == 7 and sum(t.state == "WAITING" for t in tasks) == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(len(tasks), 7)
                runtime.scheduler.cancel()
                await runtime.scheduler
                await runtime.tasks.cancel(task.id)
                await runtime.workflows.tick()
                descendants = [t for t in (await runtime.tasks.list(session.id)).items if t.id != task.id]
                self.assertTrue(all(t.cancel_requested_at is not None or t.state == "CANCELLED" for t in descendants))
                self.assertFalse(any(t.state == "QUEUED" for t in descendants))

    async def test_parent_failure_preserves_completed_child_results(self):
        with tempfile.TemporaryDirectory(prefix="oea-parent-failure-") as directory:
            async with Runtime.open(directory, RuntimeConfig(max_concurrency=2, reserved_external_slots=0),
                                    adapters={"parent": Parent("parent"), "worker": Worker()}) as runtime:
                session = await runtime.sessions.create()
                thread = await runtime.threads.create(session.id, "parent")
                task = await runtime.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="parent", payload={"fail_after_children": True}, idempotency_key="parent"))
                with self.assertRaises(RuntimeFault):
                    await runtime.tasks.result(task.id, timeout=4)
                await runtime.workflows.tick()
                batch = (await runtime.instances.list(session.id)).items[0]
                self.assertEqual(batch["state"], "SUCCEEDED")
                self.assertEqual(batch["counts"], {"SUCCEEDED": 5})

    async def test_parent_cancel_cascades_to_managed_batches(self):
        with tempfile.TemporaryDirectory(prefix="oea-parent-cancel-") as directory:
            async with Runtime.open(directory, RuntimeConfig(max_concurrency=2, reserved_external_slots=0),
                                    adapters={"parent": Parent("parent"), "worker": Worker()}) as runtime:
                session = await runtime.sessions.create()
                thread = await runtime.threads.create(session.id, "parent")
                task = await runtime.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="parent", payload={"delay": 0.2}, idempotency_key="parent"))
                for _ in range(100):
                    if (await runtime.tasks.get(task.id)).state == "WAITING":
                        break
                    await asyncio.sleep(0.01)
                batches = (await runtime.instances.list(session.id)).items
                self.assertEqual(len(batches), 1)
                await runtime.tasks.cancel(task.id)
                for _ in range(100):
                    result = await runtime.instances.get(batches[0]["id"])
                    if result["state"] == "CANCELLED":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(result["state"], "CANCELLED")

    async def test_collaboration_revocation_blocks_spawn_without_partial_batch(self):
        with tempfile.TemporaryDirectory(prefix="oea-parent-denied-") as directory:
            async with Runtime.open(directory) as runtime:
                await runtime.agents.configure(AgentConfig(id="parent", name="Parent", kind="mock", collaboration=False))
                runtime.adapters["parent"] = Parent("parent")
                runtime.adapters["worker"] = Worker()
                session = await runtime.sessions.create()
                thread = await runtime.threads.create(session.id, "parent")
                task = await runtime.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="parent", payload={}, idempotency_key="parent"))
                with self.assertRaises(RuntimeFault):
                    await runtime.tasks.result(task.id, timeout=3)
                self.assertEqual((await runtime.instances.list(session.id)).items, [])

    async def test_parent_suspends_and_children_complete_with_one_global_slot(self):
        with tempfile.TemporaryDirectory(prefix="oea-parent-") as directory:
            async with Runtime.open(directory, RuntimeConfig(max_concurrency=1, reserved_external_slots=0),
                                    adapters={"parent": Parent("parent"), "worker": Worker()}) as runtime:
                session = await runtime.sessions.create()
                thread = await runtime.threads.create(session.id, "parent")
                task = await runtime.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="parent", payload={}, idempotency_key="parent"))
                result = await runtime.tasks.result(task.id, timeout=4)
                self.assertEqual(len(result.output["children"]), 5)

    async def test_defaults_increase_capacity_without_inventing_adapter_capability(self):
        self.assertEqual(RuntimeConfig().max_concurrency, 16)
        self.assertEqual(AgentConfig(id="worker", name="Worker").max_concurrency, 4)
        self.assertEqual(AgentDescriptor(agent_id="custom").max_concurrency, 1)
