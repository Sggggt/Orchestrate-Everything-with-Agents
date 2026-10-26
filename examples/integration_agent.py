"""A v2 process Agent inspecting only the capabilities supplied by its Run.

Bind this program with Execution(program=(PYTHON, "-B", ABSOLUTE_SCRIPT_PATH)).
The host must explicitly allow commands. This example makes no model call.
"""
from oea import AgentDescriptor
from oea.stdio import ProgramResult, run_agent


class IntegrationInspector:
    def describe(self):
        return AgentDescriptor(agent_id="integration-inspector")

    async def execute(self, context):
        tools = await context.list_tools()
        instructions = await context.instructions()
        await context.progress("Inspected the capabilities supplied by this Run.")
        return ProgramResult(text="Capability inspection completed.",
                             data={"tools": tools, "instructions_available": bool(instructions)})


if __name__ == "__main__":
    run_agent(IntegrationInspector())
