"""A deterministic, network-free endpoint for exercising the public protocol."""

import asyncio

from ma_runtime.errors import AdapterFailure
from ma_runtime.models import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    CheckpointProposal,
    AgentConfig,
    MessageSpec,
    ProbeResult,
    TaskResult,
    TaskSpec,
    WaitCondition,
)


async def configure_mocks(rt):
    for number in range(1, 5):
        identity = f"mock-{number}"
        if identity not in rt.adapters:
            await rt.agents.configure(
                AgentConfig(id=identity, name=f"Mock Agent {number}", kind="mock")
            )


class MockAgent:
    def __init__(self, agent_id: str, *, max_concurrency=1):
        self.agent_id = agent_id
        self.max_concurrency = max_concurrency
        self.running = {}
        self.boundaries = {}

    def describe(self):
        return AgentDescriptor(
            agent_id=self.agent_id,
            max_concurrency=self.max_concurrency,
            checkpoint_mode="opaque_json",
            checkpoint_format_version="mock-json-v1",
            supports_suspend=True,
            supports_fork=True,
            nested_execution="runtime_mediated",
        )

    async def execute(self, request, context):
        self.running[request.run_id] = context.cancelled
        try:
            options = request.payload.get("_mock", {})
            delay = min(60, max(0, float(options.get("delay", 0.025))))
            await asyncio.sleep(delay)
            if request.attempt <= int(options.get("fail_attempts", 0)):
                raise AdapterFailure(
                    "Mock endpoint requested a retryable failure",
                    retryable=True,
                    safe_to_retry=True,
                )
            previous = request.resume_ref or {}
            wait = None
            if not previous.get("suspended_task_id") == request.task_id:
                children = []
                for child in options.get("children", []):
                    child = TaskSpec.model_validate(
                        {**child, "session_id": request.session_id}
                    )
                    children.append((await context.submit_task(child)).id)
                if children:
                    wait = WaitCondition(kind="tasks", task_ids=tuple(children))
                elif options.get("wait_for_message"):
                    wait = WaitCondition(kind="message", port="input")
                elif options.get("wait_for_input"):
                    wait = WaitCondition(kind="input")
            if wait:
                boundary = AdapterEvent(
                    type="suspended",
                    wait=wait,
                    checkpoint=CheckpointProposal(
                        format_version="mock-json-v1",
                        ref={"suspended_task_id": request.task_id},
                    ),
                    applied_message_ids=tuple(
                        m["message_id"] for m in request.messages
                    ),
                )
            else:
                text = str(request.payload.get("input", ""))
                received = [m["payload"] for m in request.messages]
                response = (
                    f"{self.agent_id}: {text}"
                    if text
                    else f"{self.agent_id}: 已完成消息处理"
                )
                if received:
                    response += "\n\n已应用协作消息：" + "；".join(
                        str(m.get("content", m)) for m in received
                    )
                if request.resume_input:
                    response += "\n\n已恢复等待任务。"
                for offset in range(0, len(response), 6):
                    if context.cancelled.is_set():
                        raise asyncio.CancelledError
                    yield AdapterEvent(
                        type="progress",
                        payload={"delta": response[offset : offset + 6]},
                    )
                    await asyncio.sleep(delay)
                outgoing = []
                for target in options.get("send_to", []):
                    outgoing.append(
                        MessageSpec(
                            session_id=request.session_id,
                            target_thread_id=target,
                            payload={"content": text},
                            idempotency_key=f"{request.task_id}:{target}",
                        )
                    )
                boundary = AdapterEvent(
                    type="completed",
                    result=TaskResult(
                        output={
                            "text": response,
                            "received_message_ids": [
                                m["message_id"] for m in request.messages
                            ],
                            "dependency_results": request.dependency_results,
                        }
                    ),
                    checkpoint=CheckpointProposal(
                        format_version="mock-json-v1",
                        ref={"last_task_id": request.task_id},
                    ),
                    applied_message_ids=tuple(
                        m["message_id"] for m in request.messages
                    ),
                    outgoing_messages=tuple(outgoing),
                )
            self.boundaries[request.run_id] = boundary
            yield boundary
        finally:
            self.running.pop(request.run_id, None)

    async def cancel(self, run_ref):
        event = self.running.get(run_ref.get("run_id"))
        if event:
            event.set()
        return CancelReceipt(accepted=True, confirmed_stopped=event is None)

    async def probe(self, run_ref):
        identity = run_ref.get("run_id")
        if identity in self.running:
            return ProbeResult(state="RUNNING")
        if identity in self.boundaries:
            return ProbeResult(
                state="COMPLETED",
                confirmed_stopped=True,
                boundary=self.boundaries[identity],
                evidence="Completed in-process boundary",
            )
        return ProbeResult(
            state="STOPPED",
            confirmed_stopped=True,
            safe_to_retry=True,
            evidence="No in-process execution exists in this host",
        )

    async def aclose(self):
        for event in self.running.values():
            event.set()
