"""Workbench permissions and durable tools using generated temporary inputs."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest

from ma_runtime import (AdapterEvent, AgentConfig, AgentDescriptor, Runtime,
                        RuntimeFault, TaskResult, ProbeResult, CancelReceipt)
from ma_runtime.workspace import Workspace


class FileAgent:
    def describe(self):
        return AgentDescriptor(agent_id="worker", max_concurrency=4)

    async def execute(self, request, context):
        results = []
        for index, call in enumerate(request.payload["calls"]):
            name, arguments = call
            results.append(await context.invoke_tool(name, arguments, f"{request.task_id}:{index}"))
        yield AdapterEvent(type="completed", result=TaskResult(output={"results": results}))

    async def aclose(self):
        pass

    async def probe(self, run_ref):
        return ProbeResult(state="STOPPED", confirmed_stopped=True)

    async def cancel(self, run_ref):
        return CancelReceipt(accepted=True, confirmed_stopped=True)


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-workbench-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        await self.rt.environments.configure({"id": "basic", "project_id": "default"})
        self.config = AgentConfig(id="worker", name="File worker", kind="external", project_id="default",
            workbench={"environment_id": "basic", "permission_profile": "workspace-write",
                       "tools": ["read_file", "write_file", "edit_file", "search_files", "publish_artifact"]})
        await self.rt.agents.configure(self.config)
        self.rt.adapters["worker"] = FileAgent()
        self.session = await self.rt.sessions.create()
        self.thread = await self.rt.threads.create(self.session.id, "worker")

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def submit(self, calls, **options):
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": self.thread.id,
            "agent_id": "worker", "payload": {"calls": calls}, "idempotency_key": "files", **options})
        try:
            return task, await self.rt.tasks.result(task.id, timeout=5)
        except RuntimeFault:
            if options.get("tool_policy", {}).get("mode") == "none":
                return task, None
            raise

    async def test_shared_gateway_and_required_success_evidence(self):
        task, result = await self.submit([
            ["write_file", {"path": "result.txt", "content": "hello"}],
            ["read_file", {"path": "result.txt"}],
            ["write_file", {"path": "result.txt", "content": "overwritten"}],
            ["publish_artifact", {"path": "result.txt"}]], tool_policy={"required": ["read_file", "write_file"]})
        values = result.output["results"]
        self.assertEqual(values[1]["text"], "hello")
        self.assertEqual(values[2]["error"]["code"], "REVISION_CONFLICT")
        artifact = await self.rt.artifacts.open(values[3]["artifact_id"])
        self.assertIn(values[3]["artifact_id"], result.artifact_ids)
        self.assertEqual(artifact.read_text(), "hello")
        provenance = (await self.rt.artifacts.get(values[3]["artifact_id"])).metadata["provenance"]
        self.assertEqual(provenance["agent_id"], "worker")
        self.assertEqual(provenance["task_id"], task.id)
        self.assertEqual(len((await self.rt.workbench.calls(task.id))["items"]), 4)
        scope = await self.rt.workbench.inspect(task_id=task.id)
        self.assertEqual(scope["revision"], 1)

    async def test_disabled_task_policy_cannot_call_native_tools(self):
        task, _ = await self.submit([["write_file", {"path": "a.txt", "content": "a"}]], tool_policy={"mode": "none"})
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "FAILED")
        self.assertEqual((await self.rt.workbench.calls(task.id))["items"], [])

    async def test_paths_links_and_revision_conflicts(self):
        scope = {"directory": str(self.root / "task"), "project_folder": "", "permission_profile": "workspace-write",
                 "environment": {"project_write": False, "resources": {}}}
        workspace = Workspace(self.rt, scope, self.session.id)
        revision = workspace.write({"path": "a.txt", "content": "one"})["revision"]
        updated = workspace.write({"path": "a.txt", "expected_revision": revision,
            "old_text": "one", "new_text": "two"}, edit=True)
        self.assertNotEqual(revision, updated["revision"])
        for name in ("../secret", "/other/secret", "C:/secret", "a.txt:stream"):
            with self.assertRaises(RuntimeFault):
                workspace.path(name)
        outside = self.root / "outside.txt"
        outside.write_text("private")
        os.link(outside, workspace.directory / "linked.txt")
        with self.assertRaises(RuntimeFault):
            workspace.path("linked.txt")
        with self.assertRaises(RuntimeFault):
            workspace.write({"path": "a.txt", "content": "stale", "expected_revision": revision})

    async def test_environment_versions_and_current_revocation(self):
        await self.rt.environments.configure({"id": "basic", "project_id": "default", "enabled": False})
        with self.assertRaises(RuntimeFault):
            await self.rt.agents.configure(self.config)
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        self.assertEqual((await self.rt.environments.get("basic"))["revision"], 2)

    async def test_revoked_environment_fails_admission_without_stopping_scheduler(self):
        await self.rt.environments.configure({"id": "basic", "project_id": "default", "enabled": False})
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": self.thread.id,
            "agent_id": "worker", "payload": {"calls": []}, "idempotency_key": "revoked"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "FAILED")
        self.assertTrue(self.rt.accepting)
        self.assertFalse(self.rt.scheduler.done())

    async def test_trusted_local_requires_host_grant_and_isolation_is_explicit(self):
        local = self.config.model_copy(update={"workbench": self.config.workbench.model_copy(
            update={"permission_profile": "trusted-local"})})
        with self.assertRaises(RuntimeFault) as denied:
            await self.rt.agents.configure(local)
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        await self.rt.environments.configure({"id": "isolated", "project_id": "default", "runner": "isolated"})
        receipt = await self.rt.environments.prepare("isolated", "prepare")
        same = await self.rt.environments.prepare("isolated", "prepare")
        self.assertEqual(receipt["task_id"], same["task_id"])
        result = await self.rt.tasks.result(receipt["task_id"], timeout=5)
        self.assertTrue(result.output["base_ready"])
        self.assertFalse(result.output["execution_ready"])
        self.assertFalse(result.output["guarantees"]["file_isolation"])

    async def test_command_cancellation_confirms_owned_process_exit_before_reconciliation(self):
        from ma_runtime.runtime import process_alive
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root / "state", allow_trusted_local=True).__aenter__()
        await self.rt.environments.configure({"id": "basic", "project_id": "default", "programs": {"python": sys.executable}})
        config = self.config.model_copy(update={"workbench": self.config.workbench.model_copy(
            update={"permission_profile": "trusted-local", "tools": ("exec_command",)})})
        await self.rt.agents.configure(config)
        self.rt.adapters["worker"] = FileAgent()
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": self.thread.id,
            "agent_id": "worker", "payload": {"calls": [["exec_command", {"argv": ["python", "-c", "import time; time.sleep(30)"]}]]},
            "idempotency_key": "cancel-command"})
        call = None
        for _ in range(200):
            page = await self.rt.workbench.calls(task.id)
            if page["items"]:
                call = await self.rt.workbench.calls(task.id, tool_call_id=page["items"][0]["id"])
                if call["operation_ref"]: break
            await asyncio.sleep(.01)
        self.assertTrue(call and call["operation_ref"])
        await self.rt.tasks.cancel(task.id)
        for _ in range(200):
            if (await self.rt.tasks.get(task.id)).state == "UNCERTAIN": break
            await asyncio.sleep(.01)
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "UNCERTAIN")
        self.assertFalse(process_alive(call["operation_ref"]["pid"]))
        with self.assertRaises(RuntimeFault):
            await self.rt.workbench.reconcile(call["id"])
        resolution = {"resolution": "CANCELLED", "evidence": "Owned process exited; command only slept and had no external action.",
                      "confirmed_stopped": True, "external_actions_confirmed": True}
        await self.rt.workbench.reconcile(call["id"], resolution)
        await self.rt.tasks.reconcile(task.id, resolution)
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "CANCELLED")


try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import InMemorySaver
    from agent.langgraph import GraphContext, LangGraphAgent, graph_wait
    LANGGRAPH = True
except ImportError:
    LANGGRAPH = False


@unittest.skipUnless(LANGGRAPH, "Install the optional langgraph extra")
class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_restart_and_replayed_node_use_original_tool_receipt(self):
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError:
            self.skipTest("Install langgraph-checkpoint-sqlite for durable graph verification")
        from typing import TypedDict
        class State(TypedDict):
            result: str
        async def replay(state, runtime):
            context = runtime.context.oea
            value = await context.invoke_tool("write_file", {"path": "once.txt", "content": "once"},
                context.request.task_id + ":stable-call")
            self.assertNotIn("isError", value)
            return {"result": graph_wait({"kind": "input"})}
        def adapter(checkpointer):
            builder = StateGraph(State, context_schema=GraphContext)
            builder.add_node("replay", replay).add_edge(START, "replay").add_edge("replay", END)
            return LangGraphAgent("graph", builder.compile(checkpointer=checkpointer),
                input_builder=lambda request: {}, managed_context=True)
        with tempfile.TemporaryDirectory(prefix="oea-graph-durable-") as root:
            root = Path(root)
            async with AsyncSqliteSaver.from_conn_string(str(root / "graph.sqlite")) as saver:
                async with Runtime.open(root / "state") as rt:
                    await rt.environments.configure({"id": "files", "project_id": "default"})
                    await rt.agents.configure(AgentConfig(id="graph", name="Graph", kind="external", project_id="default",
                        workbench={"environment_id": "files", "permission_profile": "workspace-write", "tools": ["write_file"]}))
                    rt.adapters["graph"] = adapter(saver)
                    session = await rt.sessions.create()
                    thread = await rt.threads.create(session.id, "graph")
                    task = await rt.tasks.submit({"session_id": session.id, "thread_id": thread.id,
                        "agent_id": "graph", "payload": {}, "idempotency_key": "durable"})
                    for _ in range(100):
                        snapshot = await rt.tasks.get(task.id)
                        if snapshot.state == "WAITING": break
                        await asyncio.sleep(.02)
                    self.assertEqual(snapshot.state, "WAITING")
            async with AsyncSqliteSaver.from_conn_string(str(root / "graph.sqlite")) as saver:
                async with Runtime.open(root / "state", adapters={"graph": adapter(saver)}) as rt:
                    await rt.tasks.resume(task.id, {"value": "done", "wait_token": snapshot.wait.wait_token}, "resume")
                    result = await rt.tasks.result(task.id, timeout=5)
                    self.assertEqual(result.output["result"], "done")
                    self.assertEqual(len((await rt.workbench.calls(task.id))["items"]), 1)

    async def test_context_is_ephemeral_and_wait_resumes_same_task(self):
        from typing import TypedDict
        class State(TypedDict):
            input: str
            result: str
        async def wait(state, runtime):
            self.assertIsNotNone(runtime.context.oea.request.run_id)
            value = graph_wait({"kind": "input"})
            return {"result": value}
        graph = StateGraph(State, context_schema=GraphContext).add_node("wait", wait)
        graph.add_edge(START, "wait").add_edge("wait", END)
        compiled = graph.compile(checkpointer=InMemorySaver())
        adapter = LangGraphAgent("graph", compiled, input_builder=lambda request: request.payload,
            managed_context=True, graph_version="test-v2")
        with tempfile.TemporaryDirectory(prefix="oea-graph-") as root:
            async with Runtime.open(root, adapters={"graph": adapter}) as runtime:
                session = await runtime.sessions.create()
                thread = await runtime.threads.create(session.id, "graph")
                task = await runtime.tasks.submit({"session_id": session.id, "thread_id": thread.id,
                    "agent_id": "graph", "payload": {"input": "start"}, "idempotency_key": "graph"})
                for _ in range(100):
                    snapshot = await runtime.tasks.get(task.id)
                    if snapshot.state == "WAITING":
                        break
                    await asyncio.sleep(.02)
                self.assertEqual(snapshot.state, "WAITING")
                native = await compiled.aget_state({"configurable": {"thread_id": thread.id}})
                self.assertNotIn("oea", json.dumps(native.values))
                await runtime.tasks.resume(task.id, {"value": "done", "wait_token": snapshot.wait.wait_token}, "resume")
                self.assertEqual((await runtime.tasks.result(task.id, timeout=5)).output["result"], "done")


if __name__ == "__main__":
    unittest.main()
