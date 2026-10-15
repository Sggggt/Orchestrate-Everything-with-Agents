"""Optional graph boundary adapter; graph nodes and state remain private."""

import asyncio

from ma_runtime.errors import RuntimeFault, require
from ma_runtime.models import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    CheckpointProposal,
    ProbeResult,
    TaskResult,
    WaitCondition,
)


class LangGraphAgent:
    def __init__(
        self,
        agent_id,
        graph,
        *,
        input_builder,
        output_builder=None,
        max_concurrency=1,
        message_receipts=False,
    ):
        self.agent_id, self.graph, self.input_builder = agent_id, graph, input_builder
        self.output_builder = output_builder or (lambda values: values)
        self.max_concurrency = max_concurrency
        self.receipts = message_receipts
        self.checkpointed = getattr(graph, "checkpointer", None) is not None
        self.running = {}
        self.boundaries = {}

    def describe(self):
        return AgentDescriptor(
            agent_id=self.agent_id,
            max_concurrency=self.max_concurrency,
            checkpoint_mode="native_ref" if self.checkpointed else "none",
            checkpoint_format_version="langgraph-config-v1",
            supports_suspend=self.checkpointed,
            supports_fork=False,
            message_receipts="next_turn_ack" if self.receipts else "unsupported",
            tool_mediation="none",
            nested_execution="none",
        )

    def boundary(self, values, config, task_id):
        control = values.get("_runtime", {}) if isinstance(values, dict) else {}
        checkpoint = (
            CheckpointProposal(
                mode="native_ref",
                format_version="langgraph-config-v1",
                ref={"config": config, "task_id": task_id},
            )
            if self.checkpointed
            else None
        )
        if isinstance(values, dict) and values.get("__interrupt__"):
            require(
                self.checkpointed,
                "UNSUPPORTED_CAPABILITY",
                "Interrupted graph requires a checkpointer",
            )
            return AdapterEvent(
                type="suspended",
                checkpoint=checkpoint,
                wait=WaitCondition(kind="input"),
                applied_message_ids=tuple(control.get("applied_message_ids", ())),
            )
        output = self.output_builder(values)
        require(
            isinstance(output, dict),
            "ADAPTER_PROTOCOL_ERROR",
            "Graph output builder must return JSON object",
        )
        return AdapterEvent(
            type="completed",
            result=TaskResult(output=output),
            checkpoint=checkpoint,
            applied_message_ids=tuple(control.get("applied_message_ids", ())),
            outgoing_messages=tuple(control.get("outgoing_messages", ())),
        )

    async def execute(self, request, context):
        self.running[request.run_id] = context.cancelled
        try:
            previous = request.resume_ref or {}
            config = {
                "configurable": {"thread_id": request.thread_id},
                "metadata": {
                    "runtime_run_id": request.run_id,
                    "runtime_task_id": request.task_id,
                },
            }
            if previous.get("config"):
                config["configurable"] = dict(
                    previous["config"].get("configurable", {})
                )
            await context.set_run_ref(
                {"run_id": request.run_id, "graph_config": config}
            )
            if (
                request.resume_input is not None
                and previous.get("task_id") == request.task_id
            ):
                from langgraph.types import Command

                value = Command(
                    resume=request.resume_input.get(
                        "value",
                        request.resume_input.get("content", request.resume_input),
                    )
                )
            else:
                value = self.input_builder(request)
            yield AdapterEvent(type="log", payload={"state": "graph_started"})
            values = await self.graph.ainvoke(value, config=config)
            saved_config = config
            if self.checkpointed:
                snapshot = await self.graph.aget_state(config)
                saved_config = snapshot.config
            event = self.boundary(values, saved_config, request.task_id)
            self.boundaries[request.run_id] = event
            yield event
        finally:
            self.running.pop(request.run_id, None)

    async def cancel(self, run_ref):
        pending = self.running.get(run_ref.get("run_id"))
        if pending:
            pending.set()
        return CancelReceipt(accepted=True, confirmed_stopped=pending is None)

    async def probe(self, run_ref):
        run_id = run_ref.get("run_id")
        if run_id in self.running:
            return ProbeResult(state="RUNNING")
        if run_id in self.boundaries:
            return ProbeResult(
                state="COMPLETED",
                confirmed_stopped=True,
                boundary=self.boundaries[run_id],
                evidence="Graph invocation returned a boundary",
            )
        if self.checkpointed and run_ref.get("graph_config"):
            snapshot = await self.graph.aget_state(run_ref["graph_config"])
            metadata = snapshot.metadata or {}
            if metadata.get("runtime_run_id") == run_id and not snapshot.next:
                event = self.boundary(
                    snapshot.values, snapshot.config, metadata["runtime_task_id"]
                )
                return ProbeResult(
                    state="COMPLETED",
                    confirmed_stopped=True,
                    boundary=event,
                    evidence="Native checkpoint identifies this completed run",
                )
            return ProbeResult(
                state="UNKNOWN",
                evidence="Native checkpoint does not establish this invocation outcome",
            )
        return ProbeResult(
            state="STOPPED",
            confirmed_stopped=True,
            evidence="No local graph invocation is active",
        )

    async def aclose(self):
        for event in self.running.values():
            event.set()
