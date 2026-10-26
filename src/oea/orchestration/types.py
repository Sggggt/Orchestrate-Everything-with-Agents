"""Versioned business graph definitions compiled directly into LangGraph."""

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from oea.contracts.configuration import Budget, Configuration, UsePolicy


class Position(Configuration):
    x: float = 0
    y: float = 0


class Viewport(Configuration):
    x: float = 0
    y: float = 0
    zoom: float = Field(default=1, gt=0, le=4)


class StartConfig(Configuration):
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})

    @field_validator("input_schema")
    @classmethod
    def valid_input_schema(cls, value):
        from oea.contracts.schema_validation import check_schema
        from oea.contracts.errors import RuntimeFault
        from jsonschema.exceptions import SchemaError
        try:
            check_schema(value)
        except (SchemaError, RuntimeFault):
            raise ValueError("Workflow input_schema must be valid JSON Schema") from None
        return value


class AgentNodeConfig(Configuration):
    agent_id: str
    input: str | dict = "{{inputs}}"
    role: Literal["main", "manager"] = "main"
    policy: UsePolicy = Field(default_factory=UsePolicy)


class MapConfig(Configuration):
    agent_id: str
    items: list | str | dict = "{{inputs.items}}"
    input: str | dict = "{{item}}"
    outcome: Literal["all_succeeded", "all_settled"] = "all_succeeded"
    policy: UsePolicy = Field(default_factory=UsePolicy)


class ConditionConfig(Configuration):
    left: Any = None
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "truthy"] = "truthy"
    right: Any = None


class TemplateConfig(Configuration):
    value: Any = "{{inputs}}"


class FunctionConfig(Configuration):
    name: str
    arguments: dict = Field(default_factory=dict)


class SubgraphConfig(Configuration):
    workflow_id: str
    inputs: dict = Field(default_factory=dict)


class WaitConfig(Configuration):
    prompt: str = Field(default="Provide input to continue", max_length=1024)


class EndConfig(Configuration):
    outputs: dict = Field(default_factory=dict)


class Node(Configuration):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    title: str = Field(default="", max_length=120)
    position: Position = Field(default_factory=Position)
    join: Literal["all", "any"] = "all"


class StartNode(Node):
    type: Literal["start"]
    config: StartConfig = Field(default_factory=StartConfig)


class AgentNode(Node):
    type: Literal["agent"]
    config: AgentNodeConfig


class MapNode(Node):
    type: Literal["map"]
    config: MapConfig


class ConditionNode(Node):
    type: Literal["condition"]
    config: ConditionConfig


class TemplateNode(Node):
    type: Literal["template"]
    config: TemplateConfig


class FunctionNode(Node):
    type: Literal["function"]
    config: FunctionConfig


class SubgraphNode(Node):
    type: Literal["subgraph"]
    config: SubgraphConfig


class WaitNode(Node):
    type: Literal["wait"]
    config: WaitConfig = Field(default_factory=WaitConfig)


class EndNode(Node):
    type: Literal["end"]
    config: EndConfig = Field(default_factory=EndConfig)


WorkflowNode = Annotated[StartNode | AgentNode | MapNode | ConditionNode | TemplateNode | FunctionNode | SubgraphNode | WaitNode | EndNode,
                         Field(discriminator="type")]


class Edge(Configuration):
    id: str
    source: str
    target: str
    branch: Literal["default", "true", "false"] = "default"


class WorkflowDraft(Configuration):
    nodes: tuple[WorkflowNode, ...] = Field(default=(), max_length=128)
    edges: tuple[Edge, ...] = Field(default=(), max_length=512)
    viewport: Viewport = Field(default_factory=Viewport)
    budget: Budget = Field(default_factory=Budget)
    failure_policy: Literal["fail_fast", "collect_independent"] = "fail_fast"
    max_steps: int = Field(default=512, ge=1, le=100000)


class WorkflowGraph(WorkflowDraft):
    nodes: tuple[WorkflowNode, ...] = Field(min_length=2, max_length=128)

    @model_validator(mode="after")
    def references(self):
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes) or len({edge.id for edge in self.edges}) != len(self.edges):
            raise ValueError("Node and edge IDs must be unique")
        if sum(node.type == "start" for node in self.nodes) != 1 or sum(node.type == "end" for node in self.nodes) != 1:
            raise ValueError("A declarative graph has one start and one end")
        if len({(edge.source, edge.target, edge.branch) for edge in self.edges}) != len(self.edges):
            raise ValueError("A graph has one edge for each source, target and branch")
        outgoing, incoming = {identity: [] for identity in nodes}, {identity: [] for identity in nodes}
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError("Edge endpoint does not exist")
            if nodes[edge.source].type == "condition" and edge.branch == "default":
                raise ValueError("Condition edges select true or false")
            if nodes[edge.source].type != "condition" and edge.branch != "default":
                raise ValueError("Only conditions have branch-labelled edges")
            if nodes[edge.target].type == "start" or nodes[edge.source].type == "end":
                raise ValueError("Start has no incoming edge and end has no outgoing edge")
            outgoing[edge.source].append(edge.target)
            incoming[edge.target].append(edge.source)
        def reachable(start, adjacency):
            visited, pending = set(), [start]
            while pending:
                identity = pending.pop()
                if identity not in visited:
                    visited.add(identity)
                    pending.extend(adjacency[identity])
            return visited
        start = next(node.id for node in self.nodes if node.type == "start")
        end = next(node.id for node in self.nodes if node.type == "end")
        if reachable(start, outgoing) != set(nodes) or reachable(end, incoming) != set(nodes):
            raise ValueError("Every node must be reachable from start and have a path to end")
        for node in self.nodes:
            if node.type == "condition" and {edge.branch for edge in self.edges if edge.source == node.id} != {"true", "false"}:
                raise ValueError("A condition defines both true and false paths")
            parents = set(incoming[node.id])
            if node.join == "all" and len(parents) > 1 and any(nodes[parent].type == "condition" for parent in parents):
                raise ValueError("An alternative conditional merge uses join='any'; use a code graph for mixed conditional barriers")
        return self


NODE_CONFIGS = {"start": StartConfig, "agent": AgentNodeConfig, "map": MapConfig, "condition": ConditionConfig,
                "template": TemplateConfig, "function": FunctionConfig, "subgraph": SubgraphConfig,
                "wait": WaitConfig, "end": EndConfig}


class WorkflowBuilder:
    def __init__(self):
        self.nodes, self.edges = [], []

    def node(self, identity, kind, *, title="", x=0, y=0, join="all", **config):
        self.nodes.append({"id": identity, "type": kind, "title": title, "position": {"x": x, "y": y}, "join": join, "config": config})
        return self

    def connect(self, source, target, *, branch="default"):
        self.edges.append({"id": "edge-" + str(len(self.edges)), "source": source, "target": target, "branch": branch})
        return self

    def build(self, *, failure_policy="fail_fast", budget=None, max_steps=512):
        return WorkflowGraph.model_validate({"nodes": self.nodes, "edges": self.edges, "failure_policy": failure_policy,
                                              "budget": budget or Budget(), "max_steps": max_steps})
