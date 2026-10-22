-- Rebuild the parent without the historical one-run-per-session restriction.
-- The store temporarily disables foreign keys outside this transaction and
-- checks every reference before committing; rowids preserve existing cursors.
DROP TRIGGER workflow_session_project_immutable;
CREATE TABLE workflow_runs_new(
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
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
INSERT INTO workflow_runs_new(rowid,id,workflow_id,project_id,session_id,revision,graph_json,inputs_json,state,
    idempotency_key,request_hash,result_json,error_json,cancel_requested_at,created_at,updated_at,ended_at)
SELECT rowid,* FROM workflow_runs;
DROP TABLE workflow_runs;
ALTER TABLE workflow_runs_new RENAME TO workflow_runs;
CREATE INDEX workflow_runs_workflow ON workflow_runs(workflow_id,created_at);
CREATE INDEX workflow_runs_state ON workflow_runs(state,created_at);
CREATE INDEX workflow_runs_session ON workflow_runs(session_id,created_at);
CREATE TRIGGER workflow_runs_project_insert BEFORE INSERT ON workflow_runs
WHEN NEW.project_id != (SELECT project_id FROM sessions WHERE id=NEW.session_id)
BEGIN SELECT RAISE(ABORT,'workflow session project mismatch'); END;
CREATE TRIGGER workflow_runs_identity_immutable BEFORE UPDATE OF workflow_id,project_id,session_id,revision,graph_json,inputs_json ON workflow_runs
BEGIN SELECT RAISE(ABORT,'workflow run snapshot is immutable'); END;
CREATE TRIGGER workflow_session_project_immutable BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id != OLD.project_id AND EXISTS(SELECT 1 FROM workflow_runs WHERE session_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'workflow session project is immutable'); END;

-- Thread origin and execution ownership are distinct. A later ordinary turn
-- on a workflow thread does not become part of the original run's cancel set.
CREATE TABLE thread_workflow_runs(
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    run_id TEXT NOT NULL REFERENCES workflow_runs(id)
);
CREATE INDEX thread_workflow_run ON thread_workflow_runs(run_id);
CREATE TABLE workflow_tasks(
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    run_id TEXT NOT NULL REFERENCES workflow_runs(id)
);
CREATE INDEX workflow_tasks_run ON workflow_tasks(run_id);

CREATE TEMP TABLE migrating_workflow_tasks AS
WITH RECURSIVE owned(task_id,run_id) AS (
    SELECT task_id,run_id FROM workflow_node_runs WHERE task_id IS NOT NULL
    UNION
    SELECT m.task_id,n.run_id FROM workflow_node_runs n
        JOIN instance_members m ON m.batch_id=n.batch_id
    UNION
    SELECT m.task_id,o.run_id FROM owned o
        JOIN instance_batches b ON b.parent_task_id=o.task_id
        JOIN instance_members m ON m.batch_id=b.id
)
SELECT task_id,run_id FROM owned;
INSERT INTO thread_workflow_runs
    SELECT DISTINCT t.thread_id,o.run_id FROM migrating_workflow_tasks o JOIN tasks t ON t.id=o.task_id;
INSERT INTO workflow_tasks SELECT task_id,run_id FROM migrating_workflow_tasks;
DROP TABLE migrating_workflow_tasks;

CREATE TRIGGER thread_workflow_session_insert BEFORE INSERT ON thread_workflow_runs
WHEN (SELECT session_id FROM threads WHERE id=NEW.thread_id) !=
     (SELECT session_id FROM workflow_runs WHERE id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'workflow thread session mismatch'); END;
CREATE TRIGGER thread_workflow_immutable BEFORE UPDATE ON thread_workflow_runs
BEGIN SELECT RAISE(ABORT,'workflow thread origin is immutable'); END;
CREATE TRIGGER workflow_thread_session_immutable BEFORE UPDATE OF session_id ON threads
WHEN NEW.session_id != OLD.session_id AND EXISTS(SELECT 1 FROM thread_workflow_runs WHERE thread_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'workflow thread session is immutable'); END;
CREATE TRIGGER workflow_task_owner_insert BEFORE INSERT ON workflow_tasks
WHEN NOT EXISTS(SELECT 1 FROM tasks t JOIN thread_workflow_runs h ON h.thread_id=t.thread_id
                WHERE t.id=NEW.task_id AND h.run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'workflow task owner mismatch'); END;
CREATE TRIGGER workflow_task_owner_immutable BEFORE UPDATE ON workflow_tasks
BEGIN SELECT RAISE(ABORT,'workflow task owner is immutable'); END;
