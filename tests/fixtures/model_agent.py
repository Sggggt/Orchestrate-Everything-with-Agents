"""Local process fixture for host-mediated model configuration contracts."""

from agent.stdio_sdk import run_agent
from ma_runtime import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    ProbeResult,
    TaskResult,
)
from ma_runtime.errors import RuntimeFault


class Fixture:
    def describe(self):
        return AgentDescriptor(
            agent_id="model-worker",
            execution_mode="subprocess",
            cancellation="terminate_owned_process",
            message_receipts="unsupported",
        )

    async def execute(self, request, context):
        if request.payload.get("retain_partial_failure"):
            await context.retain_partial_result(TaskResult(output={"text": "Retained process phase evidence", "facts": ["local phase"]}))
            raise RuntimeFault("LOCAL_PHASE_FAILED", "A later local phase failed")
        info = await context.llm.info()
        try:
            if request.payload.get("tamper"):
                await context._call(
                    "runtime.llm.complete",
                    request={
                        "messages": [{"role": "user", "content": "should not run"}],
                        "model": "another-model",
                        "api_key": "caller-key",
                    },
                )
            content = request.payload.get("input", "test")
            if request.payload.get("large_model_input_chars"):
                content = "x" * request.payload["large_model_input_chars"]
            reply = await context.llm.complete(
                [{"role": "user", "content": content}],
                emit_progress=True,
                allow_incomplete=request.payload.get("allow_incomplete", False),
            )
            if request.payload.get("python_failure"):
                raise ValueError("Untrusted adapter detail must not become the public error")
            if request.payload.get("repeat_read"):
                catalog = await context.list_tools()
                alias = next(name for name, identity in catalog["identifiers"].items() if identity == "mcp:fixture:echo")
                for index in range(2):
                    text = f"evidence-{index}" if request.payload["repeat_read"] == "distinct" else "same evidence"
                    if request.payload["repeat_read"] == "truncated":
                        text *= 2048
                    await context.invoke_tool(alias, {"text": text}, f"{request.task_id}:read:{index}")
                    reply = await context.llm.complete([{"role": "user", "content": "test"}])
            output = {
                "text": reply.text,
                "model": reply.model,
                "usage": reply.usage,
                "finish_reason": reply.finish_reason,
                "info": info,
            }
            artifact_ids = ()
            if request.payload.get("publish_empty"):
                artifact = await context.put_bytes("report.txt", b"Completed report")
                artifact_ids = (artifact["id"],)
            if request.payload.get("twice"):
                second = await context.llm.complete(
                    [{"role": "user", "content": "second node"}]
                )
                output["second_model"] = second.model
        except RuntimeFault as exc:
            if not request.payload.get("capture_error"):
                raise
            output = {"error_code": exc.code, "info": info}
            artifact_ids = ()
        yield AdapterEvent(type="completed", result=TaskResult(output=output, artifact_ids=artifact_ids))

    async def cancel(self, run_ref):
        return CancelReceipt(accepted=True)

    async def probe(self, run_ref):
        return ProbeResult(state="UNKNOWN")

    async def aclose(self):
        pass


run_agent(Fixture())
