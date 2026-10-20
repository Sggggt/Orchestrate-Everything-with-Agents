"""Graph checkpoint forks without node replay, including the JSONL adapter."""

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict

from ma_runtime import AgentConfig, AgentDescriptor, Runtime, RuntimeFault

LANGGRAPH = importlib.util.find_spec("langgraph") is not None


@unittest.skipUnless(LANGGRAPH, "Install the optional langgraph extra")
class GraphForkTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_graph_delivery_can_continue_from_its_checkpoint(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import InMemorySaver
        from agent.langgraph import GraphContext, LangGraphAgent
        class State(TypedDict, total=False):
            count: int
            covered_items: list[str]
        async def review(state, runtime):
            request = runtime.context.oea.request
            return {"count": state.get("count", 0) + 1, "covered_items": ["section"] if request.continuation else []}
        graph = (StateGraph(State, context_schema=GraphContext).add_node("review", review)
                 .add_edge(START, "review").add_edge("review", END).compile(checkpointer=InMemorySaver()))
        adapter = LangGraphAgent("graph", graph, input_builder=lambda request: {},
            output_builder=lambda state: state, managed_context=True)
        with tempfile.TemporaryDirectory(prefix="oea-graph-continuation-") as directory:
            async with Runtime.open(directory, adapters={"graph": adapter}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "graph")
                task = await rt.threads.turn(thread.id, "Review one section", "review", result_policy={"required_items": ["section"]})
                with self.assertRaises(RuntimeFault):
                    await rt.tasks.result(task.id, timeout=5)
                await rt.tasks.continue_result(task.id, "repair")
                result = await rt.tasks.result(task.id, timeout=5)
                self.assertEqual(result.output, {"count": 2, "covered_items": ["section"]})
                self.assertTrue((await rt.tasks.continue_result(task.id, "repair")).reused)

    async def test_embedded_fork_copies_only_safe_state_and_model_saves_preserve_adapter(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import InMemorySaver
        from agent.langgraph import GraphContext, LangGraphAgent
        class State(TypedDict, total=False):
            input: str
            count: int
            log: list[str]
            pending_calls: list
            tool_results: list
            _runtime: dict
        calls = []
        async def step(state, runtime):
            calls.append(state["input"])
            return {"count": state.get("count", 0) + 1, "log": [*state.get("log", []), state["input"]],
                    "pending_calls": [{"id": "old-call"}], "tool_results": [{"value": "old"}], "_runtime": {}}
        graph = (StateGraph(State, context_schema=GraphContext).add_node("step", step)
                 .add_edge(START, "step").add_edge("step", END).compile(checkpointer=InMemorySaver()))
        adapter = LangGraphAgent("graph", graph, input_builder=lambda request: request.payload,
            output_builder=lambda state: {"text": state["input"], "count": state["count"], "log": state["log"]},
            fork_builder=lambda state: dict(state), managed_context=True)
        self.assertTrue(adapter.describe().supports_fork)
        with tempfile.TemporaryDirectory(prefix="oea-graph-fork-") as directory:
            async with Runtime.open(directory, adapters={"graph": adapter}) as rt:
                config = AgentConfig(id="graph", name="Graph", kind="llm", base_url="http://model.invalid/v1", model="first")
                await rt.agents.configure(config)
                await rt.agents.configure(config.model_copy(update={"model": "second"}))
                self.assertIs(rt.adapters["graph"], adapter)
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "graph")
                first = await rt.threads.turn(thread.id, "first", "first")
                await rt.tasks.result(first.id, timeout=5)
                point = (await rt.tasks.get(first.id)).runs[-1].boundary_receipt["checkpoint_id"]
                second = await rt.threads.turn(thread.id, "source-only", "second")
                await rt.tasks.result(second.id, timeout=5)
                branches = await asyncio.gather(*(rt.threads.fork(thread.id, checkpoint_id=point, idempotency_key="fork") for _ in range(3)))
                self.assertEqual(len({branch.id for branch in branches}), 1)
                self.assertEqual(calls, ["first", "source-only"])
                snapshot = await graph.aget_state({"configurable": {"thread_id": branches[0].id}})
                self.assertEqual(snapshot.values["count"], 1)
                self.assertFalse(snapshot.next)
                self.assertNotIn("pending_calls", snapshot.values)
                self.assertNotIn("tool_results", snapshot.values)
                self.assertNotIn("_runtime", snapshot.values)
                child = await rt.threads.turn(branches[0].id, "branch-only", "child")
                output = await rt.tasks.result(child.id, timeout=5)
                self.assertEqual(output.output["log"], ["first", "branch-only"])
                original = await graph.aget_state({"configurable": {"thread_id": thread.id}})
                self.assertEqual(original.values["log"], ["first", "source-only"])

    async def test_waiting_graph_checkpoint_is_not_replayed_by_fork(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import InMemorySaver
        from agent.langgraph import GraphContext, LangGraphAgent, graph_wait
        class State(TypedDict, total=False):
            input: str
            text: str
        async def wait(state, runtime):
            return {"text": graph_wait({"kind": "input"})}
        graph = (StateGraph(State, context_schema=GraphContext).add_node("wait", wait)
                 .add_edge(START, "wait").add_edge("wait", END).compile(checkpointer=InMemorySaver()))
        adapter = LangGraphAgent("graph", graph, input_builder=lambda request: request.payload,
                                fork_builder=lambda state: state, managed_context=True)
        with tempfile.TemporaryDirectory(prefix="oea-graph-wait-fork-") as directory:
            async with Runtime.open(directory, adapters={"graph": adapter}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "graph")
                task = await rt.threads.turn(thread.id, "wait", "wait")
                async with asyncio.timeout(5):
                    while (await rt.tasks.get(task.id)).state != "WAITING":
                        await asyncio.sleep(0.01)
                with self.assertRaises(RuntimeFault) as fault:
                    await rt.threads.fork(thread.id, idempotency_key="pending")
                self.assertEqual(fault.exception.code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(len((await rt.sessions.snapshot(session.id))["threads"]), 1)

    async def test_process_graph_fork_uses_owned_control_child_and_persistent_checkpointer(self):
        fixture = Path(__file__).parent / "fixtures/graph_agent.py"
        with tempfile.TemporaryDirectory(prefix="oea-process-graph-fork-") as directory:
            root = Path(directory)
            async with Runtime.open(root / "state") as rt:
                descriptor = AgentDescriptor(agent_id="graph", execution_mode="subprocess", cancellation="terminate_owned_process",
                    checkpoint_mode="native_ref", checkpoint_format_version="langgraph-config-v1",
                    supports_suspend=True, supports_fork=True, tool_mediation="partial", nested_execution="runtime_mediated")
                await rt.agents.configure(AgentConfig(id="graph", name="Graph", kind="process",
                    command=(sys.executable, "-B", str(fixture.resolve()), str(root / "graph.sqlite")), process_descriptor=descriptor))
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "graph")
                first = await rt.threads.turn(thread.id, "first", "first")
                await rt.tasks.result(first.id, timeout=15)
                point = (await rt.tasks.get(first.id)).runs[-1].boundary_receipt["checkpoint_id"]
                second = await rt.threads.turn(thread.id, "source-only", "second")
                await rt.tasks.result(second.id, timeout=15)
                branch = await rt.threads.fork(thread.id, checkpoint_id=point, idempotency_key="fork")
                replayed = await rt.threads.fork(thread.id, checkpoint_id=point, idempotency_key="fork")
                self.assertEqual(branch.id, replayed.id)
                child = await rt.threads.turn(branch.id, "child", "child")
                result = await rt.tasks.result(child.id, timeout=15)
                self.assertEqual(result.output["log"], ["first", "child"])
                self.assertEqual(result.output["count"], 2)
                self.assertFalse(rt.adapters["graph"].controls)
