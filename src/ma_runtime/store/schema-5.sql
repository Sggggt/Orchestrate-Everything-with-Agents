CREATE TABLE workflows(
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','ARCHIVED')),
    revision INTEGER NOT NULL DEFAULT 1,
    graph_json TEXT NOT NULL,
    creation_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(project_id,creation_key),
    UNIQUE(id,project_id)
);
CREATE TRIGGER workflows_project_immutable BEFORE UPDATE OF project_id ON workflows
WHEN NEW.project_id != OLD.project_id
BEGIN SELECT RAISE(ABORT,'workflow project is immutable'); END;
CREATE INDEX workflows_project ON workflows(project_id,state,created_at);

CREATE TABLE workflow_runs(
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    revision INTEGER NOT NULL,
    graph_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('RUNNING','WAITING','UNCERTAIN','CANCELLING','FAILING','SUCCEEDED','FAILED','CANCELLED')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    cancel_requested_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    ended_at INTEGER,
    UNIQUE(workflow_id,idempotency_key),
    FOREIGN KEY(workflow_id,project_id) REFERENCES workflows(id,project_id)
);
CREATE INDEX workflow_runs_workflow ON workflow_runs(workflow_id,created_at);
CREATE INDEX workflow_runs_state ON workflow_runs(state,created_at);
CREATE TRIGGER workflow_runs_project_insert BEFORE INSERT ON workflow_runs
WHEN NEW.project_id != (SELECT project_id FROM sessions WHERE id=NEW.session_id)
BEGIN SELECT RAISE(ABORT,'workflow session project mismatch'); END;
CREATE TRIGGER workflow_runs_identity_immutable BEFORE UPDATE OF workflow_id,project_id,session_id,revision,graph_json,inputs_json ON workflow_runs
BEGIN SELECT RAISE(ABORT,'workflow run snapshot is immutable'); END;
CREATE TRIGGER workflow_session_project_immutable BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id != OLD.project_id AND EXISTS(SELECT 1 FROM workflow_runs WHERE session_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'workflow session project is immutable'); END;

CREATE TABLE workflow_node_runs(
    run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    node_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    task_id TEXT UNIQUE REFERENCES tasks(id),
    inputs_json TEXT,
    output_json TEXT,
    error_json TEXT,
    branch TEXT,
    started_at INTEGER,
    ended_at INTEGER,
    PRIMARY KEY(run_id,node_id)
);
