"""Declarative Agent primitives compiled directly to native LangGraph nodes."""

import hashlib
import json
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send, interrupt

from oea.contracts.architecture import ArchitectureGraph, ArchitectureNode, ArchitectureEdge
from oea.contracts.errors import RuntimeFault, require
from oea.orchestration.architecture_validation import BUILTIN_FIELDS, CONTEXT_WRITERS, TYPE_MAP, parallel_regions
from oea.orchestration.graphs.default import AgentGraphContext, AgentState, model
from oea.orchestration.graphs.templates import compare
from oea.runtime.scopes import narrow_policy, node_scope
from oea.storage.persistence import encode, event, now_ms, one


def merge(left, right):
    return {**left, **right}


def resolve(value, state):
    if isinstance(value, str) and value.startswith("$"):
        key = value[1:]
        require(key in state, "STATE_UNAVAILABLE", "Node input is unavailable", details={"field": key})
        return state[key]
    if isinstance(value, dict):
        return {key: resolve(child, state) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve(child, state) for child in value]
    return value


def combine_updates(graph, left, right):
    value = dict(left)
    for name, update in right.items():
        reducer = graph.state[name].reducer
        if name not in value or reducer == "replace":
            value[name] = update
        elif reducer == "merge":
            value[name] = {**value[name], **update}
        else:
            value[name] = value[name] + update
    return value


def isolate_parallel(graph):
    """Run each pure branch in its own native state graph until its barrier."""
    while True:
        nodes = {node.id: node for node in graph.nodes}
        regions = [(fork, join, branches) for fork, join, branches in parallel_regions(graph)
                   if join and all(nodes[key].type in {"set", "function"} for region in branches for key in region)]
        if not regions:
            return graph
        fork, join, branches = max(regions, key=lambda item: sum(map(len, item[2])))
        members = set.union(*branches)
        kept = [node for node in graph.nodes if node.id not in members]
        edges = [edge for edge in graph.edges if edge.source not in members and edge.target not in members and edge.source != fork]
        outgoing = [edge for edge in graph.edges if edge.source == fork]
        for index, (edge, region) in enumerate(zip(outgoing, branches)):
            identity = "oeaParallel" + hashlib.sha256(encode([fork, index]).encode()).hexdigest()[:24]
            require(identity not in nodes, "ARCHITECTURE_INVALID", "Node ID collides with a compiled parallel scope")
            end = identity + "End"
            branch_nodes = [nodes[key] for key in nodes if key in region] + [ArchitectureNode(id=end, type="deliver")]
            branch_edges = [ArchitectureEdge(id=item.id, source=item.source, target=end if item.target == join else item.target, branch=item.branch)
                            for item in graph.edges if item.source in region]
            branch = ArchitectureGraph(nodes=tuple(branch_nodes), edges=tuple(branch_edges), entry=edge.target if region else end,
                                       state=graph.state, max_steps=graph.max_steps)
            kept.append(ArchitectureNode(id=identity, type="subgraph", config={"graph": branch.model_dump(mode="json"), "_parallel": True}))
            edges.extend((ArchitectureEdge(id=identity + "In", source=fork, target=identity), ArchitectureEdge(id=identity + "Out", source=identity, target=join)))
        graph = graph.model_copy(update={"nodes": tuple(kept), "edges": tuple(edges)})


def state_type(graph, collect=False):
    fields = dict(AgentState.__annotations__)
    fields["_routes"] = Annotated[dict, merge]
    fields["_provided_fields"] = list[str]
    if collect:
        def reduce_updates(left, right):
            return combine_updates(graph, left, right)
        fields["_collected_updates"] = Annotated[dict, reduce_updates]
    for name, field in graph.state.items():
        value = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict, "array": list, "any": Any}[field.type]
        if field.reducer != "replace":
            value = Annotated[value, merge if field.reducer == "merge" else operator.add]
        fields[name] = value
    return TypedDict("ArchitectureState", fields, total=False)


def compile_architecture(graph, checkpointer, *, functions=None, scope="root", collect=False):
    """Compile immutable declarations; the host supplies the persistent saver."""
    graph = isolate_parallel(ArchitectureGraph.model_validate(graph))
    require(checkpointer is not None, "PERSISTENT_GRAPH_REQUIRED", "Agent architectures require the host checkpointer")
    functions = functions or {}
    builder = StateGraph(state_type(graph, collect), context_schema=AgentGraphContext)
    def initialize(state):
        provided = state.get("_provided_fields", ())
        return {name: field.default for name, field in graph.state.items() if field.default is not None and name not in provided}
    builder.add_node("_initialize", initialize)
    builder.add_edge(START, "_initialize")
    builder.add_edge("_initialize", graph.entry)

    def primitive(node):
        parallel = node.type == "subgraph" and node.config.get("_parallel", False)
        nested = compile_architecture(node.config["graph"], checkpointer, functions=functions, scope=scope + "/" + node.id, collect=parallel) if node.type == "subgraph" else None

        async def invoke(state, runtime: Runtime[AgentGraphContext]):
            context = runtime.context.oea
            task = runtime.execution_info
            activation = task.task_id
            logical_scope = scope + ":" + task.checkpoint_ns
            prefix = context.run["id"] + ":architecture:" + hashlib.sha256(encode([logical_scope, node.id, activation]).encode()).hexdigest()[:32]
            identity = (context.run["id"], logical_scope, node.id, activation)
            scoped = {**state, **{key: resolve(value, state) for key, value in node.inputs.items()}}
            def accept(db):
                context.host.tools.guard(db, context.run["id"], None)
                prior = one(db, "SELECT * FROM architecture_steps WHERE run_id=? AND scope=? AND node_id=? AND activation=?", identity)
                if prior:
                    return prior
                db.execute("INSERT INTO architecture_steps VALUES(?,?,?,?,'running',NULL,NULL,?,?)", (*identity, now_ms(), now_ms()))
                event(db, "architecture.node_started", run_id=context.run["id"], data={"scope": logical_scope, "node_id": node.id, "activation": activation})
            prior = await context.host.store.call(accept, transaction=True)
            if prior and prior["state"] == "completed" and (node.type not in CONTEXT_WRITERS or node.type == "subgraph"):
                return json.loads(prior["output_json"])
            action_prefix = state.get("context_recovery", {}).get("operation_key", "").removesuffix(":model") if node.type == "model" else ""
            action_prefix = action_prefix or prefix
            token = node_scope.set({"run_id": context.run["id"], "action_prefix": action_prefix,
                                    "policy": narrow_policy(context.policy, node.policy), "tools": node.config.get("tools", True)})
            try:
                if node.type == "model":
                    instruction = node.config.get("instruction", "")
                    field = node.config.get("instruction_field")
                    if field:
                        value = scoped.get(field)
                        require(isinstance(value, str), "STATE_TYPE_MISMATCH", "Model instruction input must be a string", node_id=node.id, details={"field": field})
                        instruction = "\n\n".join(part for part in (instruction, value) if part)
                    if instruction:
                        await context.host.contexts.append(context.run, context.instance, {"role": "user", "content": instruction}, action_prefix + ":instruction")
                    output = await model(scoped, runtime)
                elif node.type in {"prepare", "tools", "observe", "recover_context", "deliver"}:
                    method = "execute_tools" if node.type == "tools" else node.type
                    if node.type == "deliver" and node.config.get("response_field", "response_id") != "response_id":
                        scoped = {**scoped, "response_id": scoped[node.config["response_field"]]}
                    output = {} if node.type == "deliver" and scope != "root" else await getattr(context, method)(scoped)
                elif node.type == "wait":
                    waiting = scoped.get("wait") or {"kind": "input", "prompt": node.config.get("prompt", "Provide input to continue")}
                    output = await context.apply_reply(scoped, interrupt(waiting))
                elif node.type == "condition":
                    left = scoped.get(node.config["field"])
                    if node.config.get("response_text"):
                        response = await context.response(left)
                        left = response["message"].get("content") or ""
                    outcome = compare(left, node.config.get("operator", "truthy"), resolve(node.config.get("value"), scoped))
                    output = {"_routes": {node.id: "true" if outcome else "false"}}
                elif node.type == "set":
                    output = resolve(node.config.get("values", {}), scoped)
                elif node.type == "function":
                    context.authorize_extension()
                    provider = functions[node.config["name"]]
                    output = await provider.invoke({"arguments": resolve(node.config.get("arguments", {}), scoped)})
                elif node.type == "remember":
                    response = await context.response(scoped[node.config.get("response_field", "response_id")])
                    reference = await context.host.contexts.source(context.run, context.instance, response["message"].get("content") or "",
                        "architecture:" + node.config["label"])
                    output = {"reference": reference}
                elif node.type == "dispatch":
                    output = {}
                elif node.type == "subgraph":
                    child_input = {key: value for key, value in scoped.items() if key in BUILTIN_FIELDS or key in (graph.state if parallel else node.inputs)}
                    child_input["_provided_fields"] = [key for key in graph.state if key in scoped] if parallel else list(node.inputs)
                    if parallel:
                        child_input["_collected_updates"] = {}
                    output = await nested.ainvoke(child_input, context=runtime.context)
                    if parallel:
                        output = output.get("_collected_updates", {})
                    else:
                        output.pop("_provided_fields", None)
                else:
                    raise RuntimeFault("ARCHITECTURE_INVALID", "Unknown Agent primitive")
                if node.outputs:
                    require(set(node.outputs) <= output.keys(), "STATE_OUTPUT_UNAVAILABLE", "A mapped node output is missing", node_id=node.id, details={"missing": sorted(set(node.outputs) - output.keys())})
                    mapped = {target: output[source] for source, target in node.outputs.items()}
                    output = {**{key: value for key, value in output.items() if key in BUILTIN_FIELDS}, **mapped}
                elif node.type in {"function", "remember", "subgraph"} and not parallel:
                    output = {key: value for key, value in output.items() if key in BUILTIN_FIELDS}
                for key, value in output.items():
                    if key in graph.state:
                        spec = graph.state[key]
                        expected = TYPE_MAP.get(spec.type)
                        require(expected is None or isinstance(value, expected) and not (spec.type in {"integer", "number"} and isinstance(value, bool)),
                            "STATE_TYPE_MISMATCH", "Node output does not match its state type", node_id=node.id, details={"field": key, "type": spec.type})
                if collect:
                    output = {**output, "_collected_updates": {key: value for key, value in output.items() if key in graph.state}}
                encoded = encode(output)
                require(len(encoded.encode()) <= 32768, "STATE_TOO_LARGE", "Keep graph state small; persist large content as references", node_id=node.id)
                def save(db):
                    context.host.tools.guard(db, context.run["id"], None)
                    db.execute("UPDATE architecture_steps SET state='completed',output_json=?,error_json=NULL,updated_at=? WHERE run_id=? AND scope=? AND node_id=? AND activation=?", (encoded, now_ms(), *identity))
                    event(db, "architecture.node_completed", run_id=context.run["id"], data={"scope": logical_scope, "node_id": node.id, "activation": activation})
                await context.host.store.call(save, transaction=True)
                return output
            except Exception as exc:
                fault = exc.as_dict() if isinstance(exc, RuntimeFault) else {"code": "NODE_EXECUTION_FAILED", "safe_message": "Agent node failed", "node_id": node.id, "details": {"cause": type(exc).__name__}}
                await context.host.store.call(lambda db: db.execute("UPDATE architecture_steps SET state='failed',error_json=?,updated_at=? WHERE run_id=? AND scope=? AND node_id=? AND activation=?", (encode(context.host.redact(fault)), now_ms(), *identity)), transaction=True)
                raise
            finally:
                node_scope.reset(token)
        return invoke

    for node in graph.nodes:
        builder.add_node(node.id, primitive(node))
    barriers = {}
    for _, target, regions in parallel_regions(graph):
        if target:
            members = set.union(*regions)
            barriers[target] = list(dict.fromkeys(edge.source for edge in graph.edges if edge.target == target and edge.source in members))
    for node in graph.nodes:
        outgoing = [edge for edge in graph.edges if edge.source == node.id]
        if node.type == "deliver":
            builder.add_edge(node.id, END)
        elif node.type == "dispatch":
            config = node.config
            empty = "_empty_" + node.id
            def dispatch(state, dispatch_config=config, empty=empty):
                values = state.get(dispatch_config["items"], [])
                return [Send(dispatch_config["target"], {**state, dispatch_config["item_field"]: item}) for item in values] or empty
            builder.add_node(empty, lambda state: {})
            for edge in graph.edges:
                if edge.source == config["target"]:
                    builder.add_edge(empty, edge.target)
            builder.add_conditional_edges(node.id, dispatch)
        elif any(edge.branch != "default" for edge in outgoing):
            if node.type == "condition":
                route = lambda state, key=node.id: state["_routes"][key]
            else:
                route = lambda state: state["decision"]
            builder.add_conditional_edges(node.id, route, {edge.branch: edge.target for edge in outgoing})
        else:
            for edge in outgoing:
                if edge.source not in barriers.get(edge.target, ()):
                    builder.add_edge(edge.source, edge.target)
    for target, sources in barriers.items():
        builder.add_edge(sources, target)
    return builder.compile(checkpointer=checkpointer, name="oea-architecture")
