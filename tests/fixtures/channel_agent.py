"""Managed JSONL peer exercising logical reconnection and delayed frames."""

from oea import AdapterEvent, AgentDescriptor, CheckpointProposal, RuntimeFault, TaskResult
from oea.stdio import run_agent


class ChannelAgent:
    def describe(self):
        return AgentDescriptor(agent_id="channel-worker", execution_mode="subprocess",
            cancellation="terminate_owned_process", checkpoint_mode="opaque_json")

    async def execute(self, request, context):
        inbox = await context.inbox()
        positions = [item["position"] for item in inbox["items"]]
        try:
            await context.acknowledge_messages(positions[1:])
        except RuntimeFault as fault:
            gap = fault.code
        first_receipt = await context.acknowledge_messages(positions[:1])
        previous = dict(context.endpoint.channel)
        connected = await context.reconnect_channel("replacement")
        # The first response was lost to the caller. Retry its original identity.
        context.endpoint.channel = previous
        replayed = await context.reconnect_channel("replacement")
        context.endpoint.channel = previous
        try:
            await context.inbox()
        except RuntimeFault as fault:
            stale = fault.code
        finally:
            context.endpoint.channel = replayed
        remaining = await context.inbox()
        received = await context.acknowledge_messages(positions)
        yield AdapterEvent(type="completed", checkpoint=CheckpointProposal(ref={"received": positions}),
            result=TaskResult(output={"gap": gap, "stale": stale, "generation": connected["generation"],
                "original_generation": previous["generation"], "replay_generation": replayed["generation"],
                "first_receipt": first_receipt, "receipt": received, "remaining": len(remaining["items"])}),
            applied_message_ids=tuple(message["message_id"] for message in request.messages))

    async def aclose(self):
        pass


if __name__ == "__main__":
    run_agent(ChannelAgent())
