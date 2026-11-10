"""Versioned Agent execution contracts, separate from business workflows."""

from typing import Any, Literal

from pydantic import Field

from oea.contracts.configuration import Configuration, UsePolicy
from oea.orchestration.types import Node, Edge


class StateField(Configuration):
    type: Literal["string", "integer", "number", "boolean", "object", "array", "any"] = "any"
    reducer: Literal["replace", "append", "sum", "merge"] = "replace"
    default: Any = None


class ArchitectureNode(Node):
    type: Literal["prepare", "model", "tools", "observe", "recover_context", "wait", "deliver",
                  "set", "condition", "function", "remember", "dispatch", "subgraph"]
    description: str = Field(default="", max_length=640)
    config: dict = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    policy: UsePolicy = Field(default_factory=UsePolicy)


class ArchitectureEdge(Edge):
    branch: str = "default"


class ArchitectureGraph(Configuration):
    """Small typed state and capability nodes compiled into a native StateGraph."""

    format_version: Literal[1] = 1
    nodes: tuple[ArchitectureNode, ...] = Field(min_length=1, max_length=128)
    edges: tuple[ArchitectureEdge, ...] = Field(default=(), max_length=512)
    entry: str
    state: dict[str, StateField] = Field(default_factory=dict)
    max_steps: int = Field(default=512, ge=1, le=100000)
    source: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class AgentArchitecture(Configuration):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,127}$")
    project_id: str
    owner_agent_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    representation: Literal["declarative", "python"] = "declarative"
    source: dict = Field(default_factory=dict)


class PythonGraphReference(Configuration):
    graph: str = Field(min_length=1, max_length=128)
    graph_version: str = Field(min_length=1, max_length=128)


class ArchitectureOptions(Configuration):
    max_steps: int | None = Field(default=None, ge=1, le=100000)
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class ArchitectureBuilder:
    """Build a fully round-trippable declaration without starting a host."""

    def __init__(self, graph=None):
        self.value = ArchitectureGraph.model_validate(graph).model_dump(mode="json") if graph is not None else {
            "nodes": [], "edges": [], "entry": "", "state": {}}

    def state(self, name, *, type="any", reducer="replace", default=None):
        self.value["state"][name] = StateField(type=type, reducer=reducer, default=default).model_dump(mode="json")
        return self

    def node(self, identity, kind, *, title="", description="", inputs=None, outputs=None, policy=None, join="all", **config):
        self.value["nodes"].append(ArchitectureNode(id=identity, type=kind, title=title, description=description,
            inputs=inputs or {}, outputs=outputs or {}, policy=policy or UsePolicy(), join=join, config=config).model_dump(mode="json"))
        if not self.value["entry"]:
            self.value["entry"] = identity
        return self

    def connect(self, source, target, *, branch="default"):
        identities = {edge["id"] for edge in self.value["edges"]}
        index = len(identities)
        while "edge-" + str(index) in identities:
            index += 1
        self.value["edges"].append({"id": "edge-" + str(index), "source": source, "target": target, "branch": branch})
        return self

    def build(self, **options):
        return ArchitectureGraph.model_validate({**self.value, **options})
