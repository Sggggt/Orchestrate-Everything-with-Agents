"""Managed JSONL supervision fixture; no external model or business calls."""

from oea.stdio import run_agent
from oea import AdapterEvent, AgentDescriptor, CancelReceipt, CheckpointProposal, ProbeResult, TaskResult, WaitCondition


class GuidanceAgent:
    def describe(self):
        return AgentDescriptor(agent_id="guidance-worker", execution_mode="subprocess", cancellation="terminate_owned_process",
            supports_suspend=True, checkpoint_mode="opaque_json", checkpoint_format_version="guidance-v1", tool_mediation="full")

    async def execute(self, request, context):
        if request.operations_consumer_id:
            inbox = await context.operations("inbox", {"limit": 10})
            handled = []
            for item in inbox["items"]:
                if item.get("report_id"):
                    await context.operations("reply", {"report_id": item["report_id"],
                        "content": "Continue with the original operation key.", "idempotency_key": item["report_id"] + ":guide"})
                    handled.append(item["report_id"])
            await context.operations("ack", {"cursor": inbox["cursor"]})
            yield AdapterEvent(type="completed", result=TaskResult(output={"handled": handled, "text": "Guidance sent"}),
                applied_message_ids=tuple(m["message_id"] for m in request.messages))
            return
        report = await context.report_issue("Need target selection", request.task_id + ":help",
            details="The target was not selected; no external action has run.", wait=True)
        if report["wait"]:
            yield AdapterEvent(type="suspended", wait=WaitCondition.model_validate(report["wait"]),
                checkpoint=CheckpointProposal(format_version="guidance-v1", ref={"report_id": report["report_id"]}))
            return
        yield AdapterEvent(type="completed", result=TaskResult(output={"task_id": request.task_id,
            "report_id": report["report_id"], "guidance": report["guidance"]["content"], "text": "Guidance received"}),
            applied_message_ids=tuple(m["message_id"] for m in request.messages))

    async def cancel(self, run_ref):
        return CancelReceipt(accepted=True)

    async def probe(self, run_ref):
        return ProbeResult(state="UNKNOWN")

    async def aclose(self):
        pass


run_agent(GuidanceAgent())
