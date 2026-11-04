"""Static Agent graph diagnostics. Validation never executes a capability."""

import re
from typing import Any, Literal

from pydantic import Field, ValidationError

from oea.contracts.architecture import ArchitectureGraph
from oea.contracts.configuration import Configuration


class EmptyConfig(Configuration):
    pass


class ModelConfig(Configuration):
    instruction: str = Field(default="", max_length=16000)
    instruction_field: str | None = Field(default=None, max_length=64)
    tools: bool = True


class SetConfig(Configuration):
    values: dict[str, Any] = Field(default_factory=dict)


class ConditionConfig(Configuration):
    field: str
    response_text: bool = False
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "truthy"] = "truthy"
    value: Any = None


class FunctionConfig(Configuration):
    name: str
    arguments: dict = Field(default_factory=dict)


class WaitConfig(Configuration):
    prompt: str = ""


class RememberConfig(Configuration):
    label: str = Field(min_length=1, max_length=120)
    response_field: str = "response_id"


class DeliverConfig(Configuration):
    response_field: str = "response_id"


class DispatchConfig(Configuration):
    items: str
    target: str
    item_field: str


class SubgraphConfig(Configuration):
    graph: ArchitectureGraph


NODE_CONFIGS = {name: EmptyConfig for name in ("prepare", "tools", "observe", "recover_context", "deliver")}
NODE_CONFIGS.update(model=ModelConfig, set=SetConfig, condition=ConditionConfig, function=FunctionConfig,
                    wait=WaitConfig, remember=RememberConfig, deliver=DeliverConfig, dispatch=DispatchConfig, subgraph=SubgraphConfig)
CONTEXT_WRITERS = {"prepare", "model", "tools", "recover_context", "wait", "deliver", "remember", "subgraph"}
BUILTIN_FIELDS = {"run_id", "instance_id", "step", "response_id", "action_keys", "decision", "context_failure",
                  "context_recovery", "context_retries", "wait", "result_ref", "program_ref", "_activations", "_routes", "_provided_fields", "_collected_updates"}
TYPE_MAP = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def parallel_regions(graph):
    """Locate explicit forks and their nearest common merge, once per declaration."""
    links = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if edge.source in links and edge.target in links:
            links[edge.source].append(edge.target)
    regions = []
    for node in graph.nodes:
        outgoing = [edge for edge in graph.edges if edge.source == node.id]
        if node.type == "dispatch" or len(outgoing) < 2 or any(edge.branch != "default" for edge in outgoing):
            continue
        distances = []
        for edge in outgoing:
            found, pending = {}, [(edge.target, 0)]
            while pending:
                key, distance = pending.pop(0)
                if key not in found:
                    found[key] = distance
                    pending.extend((child, distance + 1) for child in links.get(key, ()))
            distances.append(found)
        common = set.intersection(*(set(found) for found in distances))
        join = min(common, key=lambda key: (max(found[key] for found in distances), sum(found[key] for found in distances), key)) if common else None
        branches = []
        for edge in outgoing:
            found, pending = set(), [edge.target]
            while pending:
                key = pending.pop()
                if key != join and key not in found:
                    found.add(key)
                    pending.extend(links.get(key, ()))
            branches.append(found)
        regions.append((node.id, join, branches))
    return regions


def validate_graph(value, *, functions=(), depth=0):
    errors, warnings = [], []
    def error(path, message, node_id=None):
        errors.append({"path": path, "node_id": node_id, "message": message})
    try:
        graph = ArchitectureGraph.model_validate(value)
    except ValidationError as exc:
        return None, [{"path": list(item["loc"]), "node_id": None, "message": item["msg"]}
                      for item in exc.errors(include_input=False)], []
    nodes = {node.id: node for node in graph.nodes}
    if len(nodes) != len(graph.nodes):
        error(["nodes"], "Node IDs must be unique")
    if len({edge.id for edge in graph.edges}) != len(graph.edges):
        error(["edges"], "Edge IDs must be unique")
    if graph.entry not in nodes:
        error(["entry"], "Entry node does not exist")
    adjacency, reverse = {key: [] for key in nodes}, {key: [] for key in nodes}
    for i, edge in enumerate(graph.edges):
        if edge.source not in nodes or edge.target not in nodes:
            error(["edges", i], "Edge endpoint does not exist")
            continue
        adjacency[edge.source].append(edge.target)
        reverse[edge.target].append(edge.source)
    def reachable(start, links):
        visited, pending = set(), list(start)
        while pending:
            key = pending.pop()
            if key in links and key not in visited:
                visited.add(key)
                pending.extend(links[key])
        return visited
    exits = {node.id for node in graph.nodes if node.type == "deliver"}
    if not exits:
        error(["nodes"], "An Agent architecture needs a delivery boundary")
    forward, backward = reachable([graph.entry], adjacency), reachable(exits, reverse)
    for name, spec in graph.state.items():
        if name in BUILTIN_FIELDS or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
            error(["state", name], "State name is reserved or invalid")
        expected = TYPE_MAP.get(spec.type)
        if spec.default is not None and expected and (not isinstance(spec.default, expected) or spec.type in {"integer", "number"} and isinstance(spec.default, bool)):
            error(["state", name, "default"], "Default does not match the state type")
        if spec.reducer == "append" and spec.type != "array" or spec.reducer == "sum" and spec.type not in {"integer", "number"} or spec.reducer == "merge" and spec.type != "object":
            error(["state", name, "reducer"], "Reducer does not match the state type")
    fields = BUILTIN_FIELDS | set(graph.state)
    def references(value, path, node_id, available=None):
        available = fields if available is None else available
        if isinstance(value, str) and value.startswith("$") and value[1:] not in available:
            error(path, "Unknown state reference", node_id)
        elif isinstance(value, dict):
            for key, child in value.items():
                references(child, path + [key], node_id, available)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                references(child, path + [index], node_id, available)

    def assignment(value, target, path, node_id):
        if target not in graph.state:
            return
        kind = graph.state[target].type
        if isinstance(value, str) and value.startswith("$"):
            source = graph.state.get(value[1:])
            if source and kind != "any" and source.type not in {kind, "any"} and not (kind == "number" and source.type == "integer"):
                error(path, "Mapped state types do not match", node_id)
        else:
            expected = TYPE_MAP.get(kind)
            if expected and (not isinstance(value, expected) or kind in {"integer", "number"} and isinstance(value, bool)):
                error(path, "Value does not match the destination state type", node_id)
    for i, node in enumerate(graph.nodes):
        base = ["nodes", i]
        if node.policy.tools.required or node.policy.skills.required:
            error(base + ["policy"], "Declare required delivery evidence on RunSpec.policy; node policies only narrow allowed capabilities", node.id)
        if node.id not in forward or node.id not in backward:
            error(base, "Node must be reachable from entry and have a delivery path", node.id)
        outgoing = [edge for edge in graph.edges if edge.source == node.id]
        if len({(edge.target, edge.branch) for edge in outgoing}) != len(outgoing):
            error(base, "Duplicate outgoing edge", node.id)
        try:
            config = NODE_CONFIGS[node.type].model_validate(node.config)
        except ValidationError as exc:
            for item in exc.errors(include_input=False):
                error(base + ["config", *item["loc"]], item["msg"], node.id)
            continue
        if node.type == "deliver" and outgoing:
            error(base, "Delivery has no outgoing edges", node.id)
        if node.type == "condition":
            if {edge.branch for edge in outgoing} != {"true", "false"} or len(outgoing) != 2:
                error(base, "A condition has exactly one true and one false edge", node.id)
            if config.field not in fields:
                error(base + ["config", "field"], "Unknown state field", node.id)
        elif node.type in {"model", "observe"}:
            allowed = {"tools", "deliver", "wait", "recover_context"} if node.type == "model" else {"model", "wait"}
            branches = [edge.branch for edge in outgoing]
            if branches != ["default"] and (set(branches) != allowed or len(branches) != len(allowed)):
                error(base, "Use one default successor or all decision branches: " + ", ".join(sorted(allowed)), node.id)
            if node.type == "model" and config.instruction_field:
                if config.instruction_field not in fields | set(node.inputs):
                    error(base + ["config", "instruction_field"], "Unknown model instruction input", node.id)
                elif config.instruction_field in graph.state and graph.state[config.instruction_field].type not in {"string", "any"}:
                    error(base + ["config", "instruction_field"], "Model instructions read string state", node.id)
        elif any(edge.branch != "default" for edge in outgoing):
            error(base, "Only model, observe and condition nodes use labelled edges", node.id)
        if node.type == "function" and config.name not in functions:
            error(base + ["config", "name"], "Register the pure function in the host", node.id)
        writes = set(node.outputs.values()) | (set(config.values) if node.type == "set" else set())
        for name in writes:
            if name not in graph.state:
                error(base + ["outputs", name], "Custom writes require a declared non-reserved state field", node.id)
        for target, source in node.inputs.items():
            if target in BUILTIN_FIELDS:
                error(base + ["inputs", target], "Input mappings cannot replace runtime-owned state", node.id)
            if isinstance(source, str) and source.startswith("$") and source[1:] not in fields:
                error(base + ["inputs", target], "Input mapping references an unknown state field", node.id)
        references(node.inputs, base + ["inputs"], node.id)
        if node.type in {"set", "condition", "function"}:
            references(node.config, base + ["config"], node.id, fields | set(node.inputs))
        if node.type == "set":
            for target, value in config.values.items():
                assignment(value, target, base + ["config", "values", target], node.id)
        if node.type in {"remember", "deliver"} and config.response_field not in fields:
            error(base + ["config", "response_field"], "Unknown response reference field", node.id)
        if node.type == "dispatch":
            target = nodes.get(config.target)
            if not target or target.type not in {"set", "function"} or config.target not in adjacency[node.id]:
                error(base + ["config", "target"], "Dispatch targets a connected pure set/function node", node.id)
            if config.items not in graph.state or graph.state[config.items].type != "array":
                error(base + ["config", "items"], "Dispatch items must name an array state field", node.id)
            if config.item_field not in graph.state:
                error(base + ["config", "item_field"], "Declare the isolated member input field", node.id)
            if len(outgoing) != 1 or target and len(adjacency[target.id]) != 1:
                error(base, "Dispatch has one member target and one merge successor", node.id)
            if target:
                for field in set(target.outputs.values()) | set(target.config.get("values", {})):
                    if field in graph.state and graph.state[field].reducer == "replace":
                        error(base, "Dynamic members must merge outputs through an explicit reducer", node.id)
        if node.type == "subgraph":
            for name in node.inputs:
                if name not in config.graph.state:
                    error(base + ["inputs", name], "Subgraph inputs name declared child state fields", node.id)
            if depth >= 8:
                error(base, "Subgraph nesting exceeds eight levels", node.id)
            else:
                _, nested_errors, nested_warnings = validate_graph(config.graph, functions=functions, depth=depth + 1)
                errors.extend({**item, "path": base + ["config", "graph"] + item["path"]} for item in nested_errors)
                warnings.extend(nested_warnings)
            for source in node.outputs:
                if source not in BUILTIN_FIELDS and source not in config.graph.state:
                    error(base + ["outputs", source], "Unknown subgraph output field", node.id)
    for fork, join, branches in parallel_regions(graph):
        base = ["nodes", next(i for i, node in enumerate(graph.nodes) if node.id == fork)]
        if join is None or nodes[join].join != "all":
            error(base, "Parallel branches need a common join='all' successor", fork)
        branch_writes = []
        for region in branches:
            if any(nodes[key].type not in {"set", "function"} for key in region if key in nodes):
                error(base, "Parallel branches contain only pure set/function nodes before their merge", fork)
            branch_writes.append(set().union(*(set(nodes[key].outputs.values()) | set(nodes[key].config.get("values", {})) for key in region if key in nodes)))
        for field in set().union(*branch_writes):
            if sum(field in writes for writes in branch_writes) > 1 and field in graph.state and graph.state[field].reducer == "replace":
                error(base, "Parallel writes to " + field + " require an explicit reducer", fork)
    if any(key in reachable(adjacency[key], adjacency) for key in nodes):
        warnings.append({"path": ["max_steps"], "message": "Cycles are bounded by max_steps and the original budget; an exit edge does not prove convergence"})
    return graph, errors, warnings
