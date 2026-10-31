"""Project aggregate deletion without changing the installed SQLite schema."""

import json

from oea.contracts.errors import require
from oea.storage.persistence import event, now_ms, one, rows


INSTANCES = "SELECT id FROM instances WHERE project_id=:project_id"
SESSIONS = "SELECT id FROM sessions WHERE project_id=:project_id"
RUNS = f"SELECT id FROM runs WHERE instance_id IN ({INSTANCES})"
WORKFLOWS = "SELECT id FROM workflows WHERE project_id=:project_id"
WORKFLOW_RUNS = f"SELECT id FROM workflow_runs WHERE workflow_id IN ({WORKFLOWS})"
STEPS = f"SELECT id FROM workflow_steps WHERE workflow_run_id IN ({WORKFLOW_RUNS})"
INTENTS = "SELECT id FROM tool_intents WHERE project_id=:project_id"
ACTIONS = f"SELECT id FROM actions WHERE run_id IN ({RUNS}) UNION ALL SELECT id FROM actions WHERE intent_id IN ({INTENTS})"
SOURCES = "SELECT id FROM context_sources WHERE project_id=:project_id"
MESSAGES = "SELECT id FROM messages WHERE project_id=:project_id"
ARTIFACTS = "SELECT id FROM artifacts WHERE project_id=:project_id"
SKILLS = "SELECT id FROM skills WHERE project_id=:project_id"
SERVERS = "SELECT id FROM mcp_servers WHERE project_id=:project_id"


def deletion_receipt(db, project_id: str, key_hash: str, input_hash: str):
    previous = one(db, "SELECT data_json FROM events WHERE project_id=? AND kind='project.deleted' AND json_extract(data_json,'$.key_sha256')=? ORDER BY position DESC LIMIT 1", (project_id, key_hash))
    if previous is None:
        return None
    data = json.loads(previous["data_json"])
    require(data["input_sha256"] == input_hash, "IDEMPOTENCY_CONFLICT", "Project deletion key has different confirmation")
    return data["receipt"]


def delete_project(db, project_id: str, confirm_name: str, key_hash: str, input_hash: str, *, assert_inactive) -> dict:
    """Delete every owned record regardless of its former execution or ledger state."""
    previous = deletion_receipt(db, project_id, key_hash, input_hash)
    if previous is not None:
        return previous
    project = one(db, "SELECT name FROM projects WHERE id=?", (project_id,))
    require(project is not None, "NOT_FOUND", "Project does not exist")
    require(confirm_name == project["name"], "CONFIRMATION_REQUIRED", "Confirm the current project name to delete it permanently")
    assert_inactive(project_id)
    params = {"project_id": project_id}
    pending = one(db, f"SELECT (SELECT COUNT(*) FROM actions WHERE id IN ({ACTIONS}) AND state NOT IN ('completed','failed')) AS actions,"
                      f"(SELECT COUNT(*) FROM usage WHERE run_id IN ({RUNS}) AND state IN ('reserved','needs_review')) AS usage", params)
    threads = [row["id"] for row in rows(db, f"SELECT id FROM runs WHERE id IN ({RUNS})", params)]
    threads.extend("workflow:" + row["id"] for row in rows(db, f"SELECT id FROM workflow_runs WHERE id IN ({WORKFLOW_RUNS})", params))
    contracts = [row["contract_id"] for row in rows(db,
        f"SELECT contract_id FROM run_tool_state WHERE run_id IN ({RUNS}) AND contract_id IS NOT NULL UNION "
        f"SELECT contract_id FROM action_contracts WHERE action_id IN ({ACTIONS}) UNION "
        f"SELECT j.value AS contract_id FROM actions a,json_each(a.input_json,'$.projection.tool_contracts') j WHERE a.run_id IN ({RUNS}) UNION "
        f"SELECT j.value AS contract_id FROM model_failures f JOIN actions a ON a.id=f.action_id,json_each(f.input_json,'$.projection.tool_contracts') j WHERE a.run_id IN ({RUNS})", params)]
    # Runs, messages and workflows contain intentional cross references. Defer their
    # checks until this one aggregate has been removed; foreign keys remain enabled.
    db.execute("PRAGMA defer_foreign_keys=ON")
    statements = (
        "DELETE FROM result_refs WHERE project_id=:project_id",
        "DELETE FROM result_snapshots WHERE project_id=:project_id",
        f"DELETE FROM run_tool_state WHERE run_id IN ({RUNS})",
        "DELETE FROM imported_transcript WHERE history_position IN (SELECT position FROM imported_history WHERE project_id=:project_id)",
        "DELETE FROM imported_links WHERE EXISTS(SELECT 1 FROM imported_history h WHERE h.project_id=:project_id AND h.migration_id=imported_links.migration_id AND h.table_name=imported_links.table_name AND CAST(json_extract(h.row_json,'$.id') AS TEXT)=imported_links.source_id)",
        "DELETE FROM imported_history WHERE project_id=:project_id",
        f"DELETE FROM skill_bodies WHERE sha256 IN (SELECT sha256 FROM skill_versions WHERE skill_id IN ({SKILLS})) AND sha256 NOT IN (SELECT sha256 FROM skill_versions WHERE skill_id NOT IN ({SKILLS}))",
        f"DELETE FROM model_failures WHERE id IN (SELECT id FROM usage WHERE run_id IN ({RUNS}))",
        f"DELETE FROM action_resolutions WHERE action_id IN ({ACTIONS})",
        f"DELETE FROM action_contracts WHERE action_id IN ({ACTIONS})",
        f"DELETE FROM skill_loads WHERE run_id IN ({RUNS}) OR source_id IN ({SOURCES})",
        f"DELETE FROM context_grants WHERE source_id IN ({SOURCES}) OR instance_id IN ({INSTANCES})",
        f"DELETE FROM message_context_refs WHERE source_id IN ({SOURCES}) OR message_id IN ({MESSAGES})",
        f"DELETE FROM conversation WHERE instance_id IN ({INSTANCES})",
        f"DELETE FROM memories WHERE instance_id IN ({INSTANCES})",
        f"DELETE FROM deliveries WHERE message_id IN ({MESSAGES}) OR instance_id IN ({INSTANCES})",
        f"DELETE FROM instance_read_positions WHERE instance_id IN ({INSTANCES})",
        f"DELETE FROM read_positions WHERE session_id IN ({SESSIONS})",
        f"DELETE FROM run_artifacts WHERE run_id IN ({RUNS}) OR artifact_id IN ({ARTIFACTS})",
        "DELETE FROM artifact_uploads WHERE project_id=:project_id",
        "DELETE FROM artifacts WHERE project_id=:project_id",
        f"DELETE FROM workflow_step_failures WHERE step_id IN ({STEPS})",
        f"DELETE FROM workflow_map_members WHERE step_id IN ({STEPS}) OR run_id IN ({RUNS}) OR instance_id IN ({INSTANCES})",
        f"DELETE FROM workflow_nodes WHERE workflow_run_id IN ({WORKFLOW_RUNS})",
        f"DELETE FROM workflow_resumptions WHERE workflow_run_id IN ({WORKFLOW_RUNS})",
        f"DELETE FROM workflow_steps WHERE workflow_run_id IN ({WORKFLOW_RUNS})",
        f"DELETE FROM run_mcp_bindings WHERE run_id IN ({RUNS}) OR server_id IN ({SERVERS})",
        f"DELETE FROM skill_versions WHERE skill_id IN ({SKILLS})",
        f"DELETE FROM mcp_catalogs WHERE server_id IN ({SERVERS})",
        f"DELETE FROM usage WHERE run_id IN ({RUNS})",
        f"DELETE FROM run_progress WHERE run_id IN ({RUNS})",
        f"DELETE FROM run_activity WHERE run_id IN ({RUNS})",
        f"DELETE FROM actions WHERE id IN ({ACTIONS})",
        "DELETE FROM tool_intents WHERE project_id=:project_id",
        "DELETE FROM context_sources WHERE project_id=:project_id",
        f"DELETE FROM run_resumptions WHERE run_id IN ({RUNS})",
        "DELETE FROM messages WHERE project_id=:project_id",
        f"DELETE FROM runs WHERE id IN ({RUNS})",
        "DELETE FROM instances WHERE project_id=:project_id",
        f"DELETE FROM workflow_runs WHERE id IN ({WORKFLOW_RUNS})",
        "DELETE FROM budgets WHERE project_id=:project_id",
        "DELETE FROM workflows WHERE project_id=:project_id",
        "DELETE FROM skills WHERE project_id=:project_id",
        "DELETE FROM mcp_servers WHERE project_id=:project_id",
        "DELETE FROM agent_definitions WHERE project_id=:project_id",
        "DELETE FROM sessions WHERE project_id=:project_id",
        "DELETE FROM events WHERE project_id=:project_id AND kind!='project.deleted'",
        "DELETE FROM projects WHERE id=:project_id",
    )
    count = 0
    for statement in statements:
        db.execute(statement, params)
        count += db.changes()
    for identity in contracts:
        db.execute("DELETE FROM tool_contracts WHERE id=? AND NOT EXISTS(SELECT 1 FROM action_contracts WHERE contract_id=?) "
                   "AND NOT EXISTS(SELECT 1 FROM run_tool_state WHERE contract_id=?) "
                   "AND NOT EXISTS(SELECT 1 FROM actions a,json_each(a.input_json,'$.projection.tool_contracts') j WHERE j.value=?) "
                   "AND NOT EXISTS(SELECT 1 FROM model_failures f,json_each(f.input_json,'$.projection.tool_contracts') j WHERE j.value=?)",
                   (identity, identity, identity, identity, identity))
        count += db.changes()
    receipt = {"project_id": project_id, "deleted": True, "deleted_at": now_ms(), "records_deleted": count,
               "pending_actions_discarded": pending["actions"], "unsettled_usage_discarded": pending["usage"]}
    event(db, "project.deleted", project_id=project_id,
          data={"key_sha256": key_hash, "input_sha256": input_hash, "receipt": receipt, "cleanup_threads": threads})
    return receipt
