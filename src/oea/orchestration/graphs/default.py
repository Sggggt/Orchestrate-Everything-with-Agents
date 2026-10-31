"""The default Agent driver: capability calls are separate graph nodes."""

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from oea.contracts.errors import RuntimeFault


class AgentState(TypedDict, total=False):
    run_id: str
    instance_id: str
    step: int
    response_id: str
    action_keys: list[str]
    decision: Literal["tools", "model", "deliver", "wait", "recover_context"]
    context_failure: dict
    context_recovery: dict
    context_retries: int
    wait: dict
    result_ref: str
    program_ref: str


class AgentExecutionContext(Protocol):
    async def prepare(self, state: AgentState) -> dict: ...
    async def call_model(self, state: AgentState) -> dict: ...
    async def recover_context(self, state: AgentState) -> dict: ...
    async def execute_tools(self, state: AgentState) -> dict: ...
    async def observe(self, state: AgentState) -> dict: ...
    async def deliver(self, state: AgentState) -> dict: ...
    async def apply_reply(self, state: AgentState, value) -> dict: ...


@dataclass(frozen=True)
class AgentGraphContext:
    """Ephemeral capabilities; LangGraph never writes this into graph state."""
    oea: AgentExecutionContext


async def prepare(state: AgentState, runtime: Runtime[AgentGraphContext]):
    return await runtime.context.oea.prepare(state)


async def model(state: AgentState, runtime: Runtime[AgentGraphContext]):
    try:
        return await runtime.context.oea.call_model(state)
    except RuntimeFault as error:
        if error.code != "MODEL_CONTEXT_REJECTED" or state.get("context_retries", 0):
            raise
        return {"decision": "recover_context", "context_failure": error.as_dict()}


async def recover_context(state: AgentState, runtime: Runtime[AgentGraphContext]):
    return await runtime.context.oea.recover_context(state)


async def tools(state: AgentState, runtime: Runtime[AgentGraphContext]):
    return await runtime.context.oea.execute_tools(state)


async def observe(state: AgentState, runtime: Runtime[AgentGraphContext]):
    return await runtime.context.oea.observe(state)


async def deliver(state: AgentState, runtime: Runtime[AgentGraphContext]):
    return await runtime.context.oea.deliver(state)


async def wait(state: AgentState, runtime: Runtime[AgentGraphContext]):
    value = interrupt(state["wait"])
    return await runtime.context.oea.apply_reply(state, value)


def compile_agent_graph(checkpointer):
    if checkpointer is None:
        raise ValueError("The default Agent requires a persistent checkpointer")
    builder = StateGraph(AgentState, context_schema=AgentGraphContext)
    for name, node in (("prepare", prepare), ("model", model), ("recover_context", recover_context), ("tools", tools), ("observe", observe), ("deliver", deliver), ("wait", wait)):
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "model")
    builder.add_conditional_edges("model", lambda state: state["decision"], {"tools": "tools", "deliver": "deliver", "wait": "wait", "recover_context": "recover_context"})
    builder.add_edge("recover_context", "model")
    builder.add_edge("tools", "observe")
    builder.add_conditional_edges("observe", lambda state: state.get("decision", "model"), {"model": "model", "wait": "wait"})
    builder.add_edge("wait", "model")
    builder.add_edge("deliver", END)
    return builder.compile(checkpointer=checkpointer, name="oea-agent-v2")
