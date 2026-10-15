"""External coordinator marker; never mirrors or launches its chat client."""

from ma_runtime.errors import RuntimeFault
from ma_runtime.models import AgentDescriptor, CancelReceipt, ProbeResult


class ExternalAgent:
    def __init__(self, agent_id="codex"):
        self.agent_id = agent_id

    def describe(self):
        return AgentDescriptor(
            agent_id=self.agent_id,
            execution_mode="external",
            supports_probe=False,
            cancellation="unsupported",
            message_receipts="unsupported",
            tool_mediation="none",
        )

    async def execute(self, request, context):
        raise RuntimeFault(
            "UNSUPPORTED_CAPABILITY", "External coordinator runs in its own client"
        )
        yield

    async def cancel(self, run_ref):
        return CancelReceipt(accepted=False)

    async def probe(self, run_ref):
        return ProbeResult(state="UNKNOWN")

    async def aclose(self):
        pass
