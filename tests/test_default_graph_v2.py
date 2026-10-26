"""Actual SQLite LangGraph nodes recover stable action references."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from oea.orchestration.graphs import AgentGraphContext, compile_agent_graph


class ControlledContext:
    def __init__(self, *, crash=False, wait=False):
        self.nodes = []
        self.effects = {}
        self.crash, self.wait = crash, wait
        self.replies = []

    async def prepare(self, state):
        self.nodes.append("prepare")
        return {"step": 0}

    async def call_model(self, state):
        self.nodes.append("model")
        if self.wait and not self.replies:
            return {"decision": "wait", "wait": {"kind": "input", "prompt": "Choose the value"}}
        if state["step"] == 0:
            return {"step": 1, "response_id": "response-reference", "action_keys": ["original-action"], "decision": "tools"}
        return {"step": 2, "response_id": "final-reference", "action_keys": [], "decision": "deliver"}

    async def execute_tools(self, state):
        self.nodes.append("tools")
        # The action service's original receipt is distinct from graph state.
        self.effects.setdefault(state["action_keys"][0], {"value": 37})
        if self.crash:
            self.crash = False
            raise RuntimeError("Crash after the action result, before graph checkpoint")
        return {}

    async def observe(self, state):
        self.nodes.append("observe")
        return {"decision": "model"}

    async def deliver(self, state):
        self.nodes.append("deliver")
        return {"result_ref": "durable-result"}

    async def apply_reply(self, state, value):
        self.replies.append(value)
        return {"decision": "model"}


class DefaultGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_graph_nodes_own_model_tool_observation_cycle(self):
        with tempfile.TemporaryDirectory(prefix="oea-v2-graph-") as directory:
            context = ControlledContext()
            async with AsyncSqliteSaver.from_conn_string(str(Path(directory) / "graph.sqlite")) as saver:
                graph = compile_agent_graph(saver)
                result = await graph.ainvoke({"run_id": "run", "instance_id": "instance"},
                    {"configurable": {"thread_id": "run"}}, context=AgentGraphContext(context))
            self.assertEqual(context.nodes, ["prepare", "model", "tools", "observe", "model", "deliver"])
            self.assertEqual(result["result_ref"], "durable-result")
            self.assertNotIn("messages", result)

    async def test_restart_resumes_the_failed_node_with_original_identity(self):
        with tempfile.TemporaryDirectory(prefix="oea-v2-graph-") as directory:
            path = str(Path(directory) / "graph.sqlite")
            config = {"configurable": {"thread_id": "run"}}
            context = ControlledContext(crash=True)
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                graph = compile_agent_graph(saver)
                with self.assertRaises(RuntimeError):
                    await graph.ainvoke({"run_id": "run", "instance_id": "instance"}, config, context=AgentGraphContext(context))
            self.assertEqual(context.nodes, ["prepare", "model", "tools"])
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                graph = compile_agent_graph(saver)
                result = await graph.ainvoke(None, config, context=AgentGraphContext(context))
                checkpoint = await graph.aget_state(config)
            self.assertEqual(context.nodes, ["prepare", "model", "tools", "tools", "observe", "model", "deliver"])
            self.assertEqual(list(context.effects), ["original-action"])
            self.assertEqual(result["run_id"], "run")
            self.assertNotIn("oea", checkpoint.values)

    async def test_input_interrupt_resumes_the_original_graph(self):
        with tempfile.TemporaryDirectory(prefix="oea-v2-graph-") as directory:
            config = {"configurable": {"thread_id": "run"}}
            context = ControlledContext(wait=True)
            async with AsyncSqliteSaver.from_conn_string(str(Path(directory) / "graph.sqlite")) as saver:
                graph = compile_agent_graph(saver)
                first = await graph.ainvoke({"run_id": "run", "instance_id": "instance"}, config, context=AgentGraphContext(context))
                self.assertTrue(first["__interrupt__"])
                second = await graph.ainvoke(Command(resume={"value": 37}), config, context=AgentGraphContext(context))
            self.assertEqual(context.replies, [{"value": 37}])
            self.assertEqual(context.nodes.count("prepare"), 1)
            self.assertEqual(second["run_id"], "run")
