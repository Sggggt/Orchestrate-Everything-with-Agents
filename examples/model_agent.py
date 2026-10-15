"""Process Agent using the model API configured in the platform UI.

Register kind="process", llm_required=True and this program's absolute command.
Configure the API URL, model and credentials in Agent settings before acceptance.
No provider URL or secret is embedded in this program.
"""

import argparse

from oea.stdio import run_agent
from oea import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    ProbeResult,
    TaskResult,
)


class ModelAgent:
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
        text = str(request.payload.get("input", ""))
        if not text:
            raise ValueError("Input text is required")
        reply = await context.llm.complete(
            [{"role": "user", "content": text}],
            emit_progress=True,
        )
        yield AdapterEvent(
            type="completed",
            result=TaskResult(
                output={
                    "text": reply.text,
                    "model": reply.model,
                    "usage": reply.usage,
                }
            ),
        )

    async def cancel(self, run_ref):
        return CancelReceipt(accepted=True)

    async def probe(self, run_ref):
        return ProbeResult(state="UNKNOWN")

    async def aclose(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", default="model-agent")
    run_agent(ModelAgent(parser.parse_args().agent_id))
