CREATE TABLE instance_batches(
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    parent_task_id TEXT REFERENCES tasks(id),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    max_concurrency INTEGER NOT NULL CHECK(max_concurrency > 0),
    routing TEXT NOT NULL,
    cancel_requested_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(session_id,idempotency_key)
);
CREATE TABLE instance_members(
    batch_id TEXT NOT NULL REFERENCES instance_batches(id),
    ordinal INTEGER NOT NULL,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    PRIMARY KEY(batch_id,ordinal)
);
CREATE INDEX instance_batches_session ON instance_batches(session_id,created_at);
CREATE INDEX instance_batches_parent ON instance_batches(parent_task_id);
ALTER TABLE workflow_node_runs ADD COLUMN batch_id TEXT REFERENCES instance_batches(id);
