"""Explicit transport projections for clients with strict legacy models."""

import json

from .errors import require
from .models import AgentConfig, WorkflowInfo, WorkflowRunSnapshot
from .store import one

VIEW_VERSIONS = ("1.0", "1.1")


def check_graph(graph):
    nodes = graph.nodes if hasattr(graph, "nodes") else graph.get("nodes", ())
    for node in nodes:
        config = node.config if hasattr(node, "config") else node.get("config", {})
        require(config.get("failure_policy", "fail_fast") == "fail_fast" and
                config.get("result_mode", "all_succeeded") == "all_succeeded",
                "VIEW_VERSION_REQUIRED", "This workflow requires response view 1.1",
                details={"view_version": "1.1"})


async def check_request(runtime, method, params, version):
    require(version in VIEW_VERSIONS, "UNSUPPORTED_VIEW_VERSION", "Supported response views are 1.0 and 1.1")
    if version != "1.0":
        return
    if method == "agents.configure":
        config = params.get("config", {})
        if isinstance(config, AgentConfig):
            identity, effort = config.id, config.reasoning_effort
        elif isinstance(config, dict):
            identity, effort = config.get("id"), config.get("reasoning_effort")
        else:
            return  # The service reports its normal request validation error.
        current = runtime.agent_configs.get(identity)
        require(effort is None and (current is None or current.reasoning_effort is None),
                "VIEW_VERSION_REQUIRED", "Explicit model reasoning settings require response view 1.1",
                details={"view_version": "1.1"})
    if not method.startswith("workflows."):
        return
    graph = params.get("graph")
    if graph:
        check_graph(graph)
    # Do not commit a legacy mutation whose response cannot be represented.
    if method in {"workflows.update", "workflows.archive", "workflows.restore"}:
        row = await runtime.store.call(lambda db: one(db, "SELECT graph_json FROM workflows WHERE id=?", (params.get("workflow_id"),)))
        if row:
            check_graph(json.loads(row.graph_json))


def project(method, value, version):
    if version != "1.0":
        return value
    if method == "resources.inspect":
        return {"resources": value["resources"]}
    if method in {"agents.list", "agents.describe", "agents.configure"}:
        items = value if isinstance(value, list) else [value]
        require(all(item.get("reasoning_effort") is None for item in items),
                "VIEW_VERSION_REQUIRED", "Explicit model reasoning settings require response view 1.1",
                details={"view_version": "1.1"})
    if isinstance(value, (WorkflowInfo, WorkflowRunSnapshot)):
        check_graph(value.graph)
    if method == "workflows.node_types":
        for node in value:
            key = {"start": "failure_policy", "map": "result_mode"}.get(node["type"])
            if key:
                node["defaults"].pop(key, None)
                node["config_schema"]["properties"].pop(key, None)
    return value
