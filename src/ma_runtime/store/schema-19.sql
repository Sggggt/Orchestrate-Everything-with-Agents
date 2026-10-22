CREATE TABLE workflow_repairs (
    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id),
    source_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    rerun_nodes_json TEXT NOT NULL,
    invalidated_nodes_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE workflow_reused_nodes (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    source_node_id TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    PRIMARY KEY(run_id,node_id),
    FOREIGN KEY(run_id,node_id) REFERENCES workflow_node_runs(run_id,node_id),
    FOREIGN KEY(source_run_id,source_node_id) REFERENCES workflow_node_runs(run_id,node_id)
);
CREATE TABLE workflow_repair_members (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    source_run_id TEXT REFERENCES workflow_runs(id),
    result_hash TEXT,
    PRIMARY KEY(run_id,node_id,ordinal),
    FOREIGN KEY(run_id,node_id) REFERENCES workflow_node_runs(run_id,node_id)
);
CREATE TRIGGER workflow_terminal_node_immutable BEFORE UPDATE ON workflow_node_runs
WHEN OLD.state IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') BEGIN
    SELECT RAISE(ABORT,'terminal workflow nodes are immutable');
END;
