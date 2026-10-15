"""Runnable Agent SDK example: inspect profile and invoke a granted MCP tool.

Register with kind=process and command=[PYTHON, '-B', ABSOLUTE_PATH_TO_THIS_FILE].
The default Agent ID is integration-inspector; pass --agent-id to change it.
"""

import argparse

from oea.stdio import run_agent
from oea import AdapterEvent, AgentDescriptor, TaskResult
from oea import CancelReceipt, ProbeResult


class IntegrationInspector:
    def __init__(self, agent_id):
        self.agent_id = agent_id

    def describe(self):
        return AgentDescriptor(
            agent_id=self.agent_id,
            execution_mode="subprocess",
            cancellation="terminate_owned_process",
            message_receipts="unsupported",
        )

    async def execute(self, request, context):
        yield AdapterEvent(
            type="progress",
            payload={"delta": "正在检查当前 Agent 的指令、技能与工具。\n"},
        )
        skills = await context.list_skills()
        tools = await context.list_tools()
        loaded = await context.read_skill(skills[0]["id"]) if skills else None
        output = {
            "skills": skills,
            "agent_md_loaded": bool(request.agent_md),
            "tools": [t["function"]["name"] for t in tools["tools"]],
            "skill_loaded": loaded["path"] if loaded else None,
        }
        if request.payload.get("tool"):
            output["tool_result"] = await context.invoke_tool(
                request.payload["tool"],
                request.payload.get("arguments", {}),
                request.task_id + ":tool",
            )
        output["text"] = (
            f"已加载 AGENT.md：{bool(request.agent_md)}；可用技能 {len(skills)} 项；MCP 工具 {len(tools['tools'])} 个。"
        )
        yield AdapterEvent(type="completed", result=TaskResult(output=output))

    async def cancel(self, run_ref):
        return CancelReceipt(accepted=True)

    async def probe(self, run_ref):
        return ProbeResult(state="UNKNOWN")

    async def aclose(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", default="integration-inspector")
    run_agent(IntegrationInspector(parser.parse_args().agent_id))
