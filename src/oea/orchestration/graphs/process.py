"""Native process turn, durable suspension, and validated delivery nodes."""

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from oea.orchestration.graphs.default import AgentGraphContext, AgentState
from oea.contracts.errors import RuntimeFault, require
from oea.stdio.agent import PROGRAM_TOOL


async def prepare(state, runtime: Runtime[AgentGraphContext]):
    context = runtime.context.oea
    return await context.prepare(state, uses_model=context.definition.execution.program_uses_model)


async def execute(state, runtime: Runtime[AgentGraphContext]):
    context = runtime.context.oea
    step = state.get("step", 0)
    request_id = context.resumption["id"] if context.resumption else "initial"
    key = context.run["id"] + ":program:" + str(step) + ":" + request_id
    result = await context.host.tools.invoke(context.run["id"], PROGRAM_TOOL, {"checkpoint_ref": state.get("program_ref")}, key)
    require(not result.get("isError"), "PROGRAM_FAILED", "Process turn did not complete", details=result)
    receipt = await context.host.store.call(lambda db: next(iter(db.execute("SELECT id FROM actions WHERE run_id=? AND operation_key=?", (context.run["id"], key)))))
    return {"step": step + 1, "program_ref": receipt["id"], "decision": "wait" if result["status"] == "suspended" else "deliver", "wait": result.get("wait") or {}}


async def wait(state, runtime: Runtime[AgentGraphContext]):
    value = interrupt(state["wait"])
    return await runtime.context.oea.apply_reply(state, value)


async def deliver(state, runtime: Runtime[AgentGraphContext]):
    context = runtime.context.oea
    result = await context.response(state["program_ref"])
    return await context.complete_result(state["program_ref"], result["text"], "stop", data=result["data"], origin="program")


def compile_process_graph(checkpointer):
    builder = StateGraph(AgentState, context_schema=AgentGraphContext)
    for name, node in (("prepare", prepare), ("program", execute), ("wait", wait), ("deliver", deliver)):
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "program")
    builder.add_conditional_edges("program", lambda value: value["decision"], {"wait": "wait", "deliver": "deliver"})
    builder.add_edge("wait", "program")
    builder.add_edge("deliver", END)
    return builder.compile(checkpointer=checkpointer, name="oea-process-v2")
