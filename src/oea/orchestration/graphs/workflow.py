"""Declarative topology-to-LangGraph mapping; no independent DAG driver."""

from dataclasses import dataclass
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send, interrupt

from oea.orchestration.graphs.workflow_context import WorkflowContext


def merge(previous, update):
    value = dict(previous or {})
    for key, item in (update or {}).items():
        if item is None:
            value.pop(key, None)
        else:
            value[key] = item
    return value


class WorkflowState(TypedDict, total=False):
    workflow_run_id: str
    refs: Annotated[dict[str, str], merge]
    versions: Annotated[dict[str, str], merge]
    iterations: Annotated[dict[str, int], merge]
    routes: Annotated[dict[str, str], merge]
    failures: Annotated[dict[str, str], merge]
    dispatch: Annotated[dict, merge]
    dispatched: Annotated[dict, merge]
    ready: Annotated[dict, merge]
    result_ref: str
    recovery_request_id: str


class MapItem(TypedDict):
    workflow_run_id: str
    step_id: str
    index: int


@dataclass(frozen=True)
class WorkflowGraphContext:
    oea: WorkflowContext


def ordinary(node):
    async def execute(state: WorkflowState, runtime: Runtime[WorkflowGraphContext]):
        return await runtime.context.oea.execute(node, state)
    return execute


def dispatch(node):
    async def execute(state: WorkflowState, runtime: Runtime[WorkflowGraphContext]):
        context = runtime.context.oea
        try:
            if node.type == "agent":
                return await context.dispatch_agent(node, state)
            if node.type == "map":
                return await context.prepare_map(node, state)
            return await context.dispatch_subgraph(node, state)
        except Exception as error:
            return await context.dispatch_error(node, state, error)
    return execute


def member(node):
    async def execute(state: MapItem, runtime: Runtime[WorkflowGraphContext]):
        try:
            return await runtime.context.oea.map_member(node, state)
        except Exception as error:
            return await runtime.context.oea.map_member_error(state, error)
    return execute


def child_wait(node):
    async def execute(state: WorkflowState, runtime: Runtime[WorkflowGraphContext]):
        if state["dispatch"][node.id].get("skipped"):
            return {"ready": {node.id: True}}
        context = runtime.context.oea
        if state["dispatch"][node.id].get("cached"):
            return await context.cached_dispatch(node, state)
        _, descriptor = await context.wait_descriptor(node, state)
        if descriptor.get("run_ids") or descriptor.get("workflow_run_ids"):
            # Exactly one durable wait per node invocation. The host observes
            # child receipts and supplies a wake-up; the node checks them again.
            interrupt(descriptor)
        return await context.settle(node, state)
    return execute


def input_wait(node):
    async def execute(state: WorkflowState, runtime: Runtime[WorkflowGraphContext]):
        context = runtime.context.oea
        step, cached = await context.prepare_input(node, state)
        if cached is not None:
            return cached
        value = interrupt({"kind": "input", "prompt": node.config.prompt, "node_id": node.id, "step_id": step["id"]})
        return await context.accept_input(node, state, step, value)
    return execute


def names(node):
    prefix = "__oea__" + node.id
    return prefix + "__wait", prefix + "__done", prefix + "__item"


def route_map(node, state):
    wait, _, worker = names(node)
    item = state["dispatch"][node.id]
    if item.get("skipped") or item.get("cached") or not item["count"]:
        return wait
    return [Send(worker, {"workflow_run_id": state["workflow_run_id"], "step_id": item["step_id"], "index": index}) for index in range(item["count"])]


def done(node):
    async def execute(state: WorkflowState):
        step_id = state["dispatch"][node.id]["step_id"]
        return {"dispatch": {node.id: None}, "ready": {node.id: None}, "dispatched": {step_id: None}}
    return execute


def compile_workflow(definition, checkpointer):
    builder = StateGraph(WorkflowState, context_schema=WorkflowGraphContext)
    finish = {}
    for node in definition.nodes:
        metadata = {"oea_node_id": node.id, "kind": node.type}
        if node.type in {"agent", "map", "subgraph"}:
            wait, completed, worker = names(node)
            builder.add_node(node.id, dispatch(node), metadata=metadata)
            builder.add_node(wait, child_wait(node), metadata={**metadata, "phase": "wait"})
            builder.add_node(completed, done(node), metadata={**metadata, "phase": "done"})
            builder.add_conditional_edges(wait, lambda state, identity=node.id: "done" if state.get("ready", {}).get(identity) else "again", {"done": completed, "again": wait})
            if node.type == "map":
                builder.add_node(worker, member(node), input_schema=MapItem, metadata={**metadata, "phase": "dispatch_item"})
                builder.add_conditional_edges(node.id, lambda state, selected=node: route_map(selected, state), {wait: wait, worker: worker})
                builder.add_edge(worker, wait)
            else:
                builder.add_edge(node.id, wait)
            finish[node.id] = completed
        elif node.type == "wait":
            builder.add_node(node.id, input_wait(node), metadata=metadata)
            finish[node.id] = node.id
        else:
            builder.add_node(node.id, ordinary(node), metadata=metadata)
            finish[node.id] = node.id
    for node in definition.nodes:
        if node.type == "start":
            builder.add_edge(START, node.id)
        if node.type == "end":
            builder.add_edge(node.id, END)
        if node.type == "condition":
            targets = [edge for edge in definition.edges if edge.source == node.id]
            def route(state, identity=node.id, edges=targets):
                return [edge.target for edge in edges if edge.branch == state.get("routes", {}).get(identity)]
            builder.add_conditional_edges(node.id, route, {edge.target: edge.target for edge in targets})
    for node in definition.nodes:
        incoming = [edge for edge in definition.edges if edge.target == node.id and next(source for source in definition.nodes if source.id == edge.source).type != "condition"]
        if len(incoming) > 1 and node.join == "all":
            builder.add_edge([finish[edge.source] for edge in incoming], node.id)
        else:
            for edge in incoming:
                builder.add_edge(finish[edge.source], node.id)
    return builder.compile(checkpointer=checkpointer, name="oea-workflow-v2")
