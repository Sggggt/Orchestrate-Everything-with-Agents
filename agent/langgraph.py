"""Optional graph boundary adapter; graph nodes and state remain private."""

import asyncio
from dataclasses import dataclass
import json

from ma_runtime.errors import RuntimeFault, require
from ma_runtime.context import RunContext
from ma_runtime.models import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    CheckpointProposal,
    ProbeResult,
    TaskResult,
    WaitCondition,
    GraphAdapterConfig,
)


@dataclass(frozen=True)
class GraphContext:
    """Per-invocation capabilities; never store this object in graph state."""
    oea: RunContext


def graph_wait(condition):
    """Use in a dedicated node after persisting dispatch/call identities."""
    from langgraph.types import interrupt
    wait = WaitCondition.model_validate(condition)
    return interrupt({"oea_wait": wait.model_dump(mode="json")})


async def managed_tool_node(state, runtime):
    """Execute persisted OpenAI function calls sequentially through RunContext."""
    from ma_runtime.workspace import tool_messages
    context = runtime.context.oea
    results, messages, images = [], [], []
    for call in state.get("pending_calls", []):
        require(call.get("id"), "INVALID_REQUEST", "Persist a stable tool call ID before execution")
        function = call["function"]
        arguments = function["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        value = await context.invoke_tool(function["name"], arguments,
            f"{context.request.task_id}:graph:{call['id']}", model_call_id=state.get("model_call_id"), tool_call_id=call["id"])
        results.append({"call_id": call["id"], "result": value})
        response = tool_messages(call["id"], value)
        messages.append(response[0])
        images.extend(response[1:])
    return {"tool_results": results, "messages": messages + images}


class LangGraphAgent:
    def __init__(
        self,
        agent_id,
        graph,
        *,
        input_builder,
        output_builder=None,
        fork_builder=None,
        max_concurrency=1,
        message_receipts=False,
        managed_context=False,
        recursion_limit=24,
        graph_version="1.0",
    ):
        self.agent_id, self.graph, self.input_builder = agent_id, graph, input_builder
        self.output_builder = output_builder or (lambda values: values)
        self.fork_builder = fork_builder
        self.max_concurrency = max_concurrency
        self.receipts = message_receipts
        self.checkpointed = getattr(graph, "checkpointer", None) is not None
        self.running = {}
        self.boundaries = {}
        options = GraphAdapterConfig(managed_context=managed_context, recursion_limit=recursion_limit, graph_version=graph_version)
        self.managed_context = options.managed_context
        self.recursion_limit = options.recursion_limit
        self.graph_version = options.graph_version
        self.invocations = {}

    def describe(self):
        return AgentDescriptor(
            agent_id=self.agent_id,
            adapter_version=self.graph_version,
            max_concurrency=self.max_concurrency,
            checkpoint_mode="native_ref" if self.checkpointed else "none",
            checkpoint_format_version="langgraph-config-v1",
            supports_suspend=self.checkpointed,
            supports_fork=bool(self.checkpointed and self.managed_context and self.fork_builder and "__start__" in self.graph.nodes),
            message_receipts="next_turn_ack" if self.receipts else "unsupported",
            tool_mediation="partial" if self.managed_context else "none",
            nested_execution="runtime_mediated" if self.managed_context else "none",
        )

    async def fork_checkpoint(self, reference, thread_id):
        """Seed a fresh graph thread without executing any node or pending action."""
        from langgraph.graph import START, END
        require(self.describe().supports_fork, "UNSUPPORTED_CAPABILITY", "This graph has no safe state fork builder")
        snapshot = await self.graph.aget_state(reference["config"])
        require(snapshot.config.get("configurable", {}).get("checkpoint_id") and not snapshot.next
                and not any(task.interrupts for task in snapshot.tasks),
                "UNSUPPORTED_CAPABILITY", "Fork only a completed graph checkpoint; resume pending work in its original task")
        target = {"configurable": {"thread_id": thread_id}}
        existing = await self.graph.aget_state(target)
        if existing.config.get("configurable", {}).get("checkpoint_id"):
            if not existing.next:
                return {"config": existing.config, "task_id": reference.get("task_id")}
            seeded = existing.config
        else:
            state = self.fork_builder(snapshot.values)
            require(isinstance(state, dict), "ADAPTER_PROTOCOL_ERROR", "Graph fork builder must return a business-state object")
            state = {key: value for key, value in state.items() if key not in {"_runtime", "pending_calls", "tool_results", "model_call_id", "__interrupt__"}}
            seeded = await self.graph.aupdate_state(target, state, as_node=START)
        cleared = await self.graph.aupdate_state(seeded, None, as_node=END)
        return {"config": cleared, "task_id": reference.get("task_id")}

    def boundary(self, values, config, task_id):
        control = values.get("_runtime", {}) if isinstance(values, dict) else {}
        checkpoint = (
            CheckpointProposal(
                adapter_version=self.graph_version,
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
            interrupts = values["__interrupt__"]
            require(len(interrupts) == 1, "ADAPTER_PROTOCOL_ERROR", "Use one managed wait boundary at a time")
            value = interrupts[0].value
            wait = WaitCondition.model_validate(value["oea_wait"]) if self.managed_context and isinstance(value, dict) and "oea_wait" in value else WaitCondition(kind="input")
            return AdapterEvent(
                type="suspended",
                checkpoint=checkpoint,
                wait=wait,
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
                "recursion_limit": self.recursion_limit,
                "configurable": {"thread_id": request.thread_id},
                "metadata": {
                    "runtime_run_id": request.run_id,
                    "runtime_task_id": request.task_id,
                    "runtime_graph_version": self.graph_version,
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
                and not request.continuation
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
            invocation = asyncio.create_task(self.graph.ainvoke(value, config=config,
                **({"context": GraphContext(oea=context)} if self.managed_context else {})))
            self.invocations[request.run_id] = invocation
            cancellation = asyncio.create_task(context.cancelled.wait())
            try:
                done, _ = await asyncio.wait({invocation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
                if cancellation in done:
                    invocation.cancel()
                    await asyncio.gather(invocation, return_exceptions=True)
                    raise asyncio.CancelledError
                values = await invocation
            finally:
                cancellation.cancel()
                if not invocation.done():
                    invocation.cancel()
                    await asyncio.gather(invocation, return_exceptions=True)
                await asyncio.gather(cancellation, return_exceptions=True)
                self.invocations.pop(request.run_id, None)
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
        if self.invocations:
            await asyncio.gather(*self.invocations.values(), return_exceptions=True)
