"""Runtime ownership checks and cache retirement for project deletion."""

import asyncio
import json

from oea.contracts.errors import require
from oea.storage.persistence import encode, event, now_ms, one, rows
from oea.mcp.upstream import UpstreamTool


def assert_project_inactive(host, project_id):
    active = any(context.instance["project_id"] == project_id for context in tuple(host.active.values()))
    discovering = project_id in tuple(host.mcp.discovering.values())
    receiving = project_id in tuple(host.artifacts.receiving_projects.values())
    calling = project_id in tuple(host.mcp.calling.values())
    require(not (active or discovering or receiving or calling), "PROJECT_BUSY", "Wait for this project's running work, discovery and uploads to finish")


async def prepare_project_deletion(host, project_id):
    host.assert_open()
    def read(db):
        require(one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,)), "NOT_FOUND", "Project does not exist")
        return {"instances": {row["id"] for row in rows(db, "SELECT id FROM instances WHERE project_id=?", (project_id,))},
                "runs": {row["id"] for row in rows(db, "SELECT r.id FROM runs r JOIN instances i ON i.id=r.instance_id WHERE i.project_id=?", (project_id,))},
                "workflows": {row["id"] for row in rows(db, "SELECT r.id FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id WHERE w.project_id=?", (project_id,))},
                "intents": {row["id"] for row in rows(db, "SELECT id FROM tool_intents WHERE project_id=?", (project_id,))}}
    scope = await host.store.call(read)
    host.project_deletion_scopes[project_id] = scope
    def revoke(db):
        db.execute("UPDATE runs SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE instance_id IN (SELECT id FROM instances WHERE project_id=?) AND state IN ('accepted','running','waiting','needs_review')", (now_ms(), project_id))
        db.execute("UPDATE workflow_runs SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE workflow_id IN (SELECT id FROM workflows WHERE project_id=?) AND state IN ('accepted','running','waiting','needs_review')", (now_ms(), project_id))
        db.execute("UPDATE tool_intents SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE project_id=?", (now_ms(), project_id))
        event(db, "project.deletion_requested", project_id=project_id)
    await host.store.call(revoke, transaction=True)
    tasks = {task for mapping, ids in ((host.jobs, scope["runs"]), (host.dependencies.jobs, scope["runs"]),
             (host.workflows.jobs, scope["workflows"]), (host.workflows.waiters, scope["workflows"]))
             for identity, task in tuple(mapping.items()) if identity in ids}
    tasks.update(task for task, owner in tuple(host.artifacts.receiving_projects.items()) if owner == project_id)
    for mapping in (host.mcp.discovering, host.mcp.calling):
        tasks.update(task for task, owner in tuple(mapping.items()) if owner == project_id)
    tasks.update(task for identity, task in tuple(host.workflows.computations.items())
                 if host.workflows.computation_owners.get(identity) in scope["workflows"])
    tasks.discard(asyncio.current_task())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    owners = scope["runs"] | scope["intents"]
    pending = {owner for owner in host.tools.owners.values() if owner in owners}
    if pending:
        await asyncio.gather(*(host.tools.cancel(owner) for owner in pending))
    # Any uncancellable external work retains its real lifetime. Deletion revokes
    # the local execution identity; its eventual result cannot recreate records.
    for identity in scope["runs"]:
        host.active.pop(identity, None)
        host.reviewing.discard(identity)
    assert_project_inactive(host, project_id)


async def on_project_deleted(host, project_id):
    scope = host.project_deletion_scopes.get(project_id, {"instances": set(), "runs": set(), "workflows": set()})
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
        host.notify_run(identity)
        host.reviewing.discard(identity)
    for identity in scope["workflows"]:
        host.workflows.signals.pop(identity, None)
        host.workflows.reviewing.discard(identity)
    if connections:
        await asyncio.gather(*(connection.aclose() for connection in connections))
    cleanup = await host.store.call(lambda db: one(db, "SELECT position,data_json FROM events WHERE project_id=? AND kind='project.deleted' AND json_type(data_json,'$.cleanup_threads')='array' ORDER BY position DESC LIMIT 1", (project_id,)))
    if cleanup:
        data = json.loads(cleanup["data_json"])
        for thread_id in data.pop("cleanup_threads"):
            await host.saver.adelete_thread(thread_id)
        await host.store.call(lambda db: db.execute("UPDATE events SET data_json=? WHERE position=?", (encode(data), cleanup["position"])), transaction=True)
    host.project_deletion_scopes.pop(project_id, None)
