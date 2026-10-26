"""Runtime ownership checks and cache retirement for project deletion."""

import asyncio
import json

from oea.contracts.errors import require
from oea.storage.persistence import one, rows
from oea.mcp.upstream import UpstreamTool


def assert_project_inactive(host, project_id):
    active = any(context.instance["project_id"] == project_id for context in tuple(host.active.values()))
    discovering = project_id in tuple(host.mcp.discovering.values())
    receiving = project_id in tuple(host.artifacts.receiving_projects.values())
    calling = project_id in tuple(host.mcp.calling.values())
    require(not (active or discovering or receiving or calling), "PROJECT_BUSY", "Wait for this project's running work, discovery and uploads to finish")


async def assert_project_idle(host, project_id):
    host.assert_open()
    def read(db):
        require(one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,)), "NOT_FOUND", "Project does not exist")
        return {"instances": {row["id"] for row in rows(db, "SELECT id FROM instances WHERE project_id=?", (project_id,))},
                "runs": {row["id"] for row in rows(db, "SELECT r.id FROM runs r JOIN instances i ON i.id=r.instance_id WHERE i.project_id=?", (project_id,))},
                "workflows": {row["id"] for row in rows(db, "SELECT r.id FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id WHERE w.project_id=?", (project_id,))}}
    scope = await host.store.call(read)
    assert_project_inactive(host, project_id)
    jobs = any(not task.done() for identity, task in tuple(host.jobs.items()) if identity in scope["runs"])
    jobs = jobs or any(not task.done() for identity, task in tuple(host.workflows.jobs.items()) if identity in scope["workflows"])
    jobs = jobs or any(not task.done() for identity, task in tuple(host.workflows.computations.items())
                       if host.workflows.computation_owners.get(identity) in scope["workflows"])
    require(not jobs, "PROJECT_BUSY", "Wait for this project's execution to actually stop")
    host.project_deletion_scopes[project_id] = scope


async def on_project_deleted(host, project_id):
    scope = host.project_deletion_scopes.pop(project_id, {"instances": set(), "runs": set(), "workflows": set()})
    if await host.store.call(lambda db: one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,))):
        return
    for identity, definition in tuple(host.definitions.items()):
        if definition.project_id == project_id:
            host.definitions.pop(identity, None)
    for identity, (definition, _) in tuple(host.skills.definitions.items()):
        if definition.project_id == project_id:
            host.skills.definitions.pop(identity, None)
    servers = {identity for identity, (definition, _) in tuple(host.mcp.configurations.items()) if definition.project_id == project_id}
    for identity in servers:
        host.mcp.configurations.pop(identity, None)
        host.mcp.retiring.discard(identity)
    for name, provider in tuple(host.tools.providers.items()):
        if isinstance(provider, UpstreamTool) and provider.server_id in servers:
            host.tools.providers.pop(name, None)
            host.tools.validators.pop(name, None)
            host.tools.contracts.pop(name, None)
    connections = []
    for key, connection in tuple(host.mcp.connections.items()):
        owned = key[0] in servers or connection.definition.project_id == project_id
        if not owned and key[2].startswith("["):
            consumer = json.loads(key[2])
            owned = (consumer[0] == "controller" and consumer[1] == project_id
                     or consumer[0] == "instance" and consumer[1] in scope["instances"])
        if owned:
            host.mcp.connections.pop(key, None)
            connections.append(connection)
    for identity in scope["runs"]:
        host.run_changes.pop(identity, None)
        host.reviewing.discard(identity)
    for identity in scope["workflows"]:
        host.workflows.signals.pop(identity, None)
        host.workflows.reviewing.discard(identity)
    if connections:
        await asyncio.gather(*(connection.aclose() for connection in connections))
