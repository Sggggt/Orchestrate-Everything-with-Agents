ALTER TABLE tasks ADD COLUMN continuation_runs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tool_calls ADD COLUMN parent_stage_id TEXT;
ALTER TABLE context_access ADD COLUMN first_run_id TEXT REFERENCES runs(id);
CREATE TABLE task_continuations (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    runs_used INTEGER NOT NULL,
    calls_used INTEGER NOT NULL,
    tokens_reserved INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    deadline_at INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    append_output INTEGER NOT NULL
);
CREATE TABLE task_continuation_receipts (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(task_id,idempotency_key)
);
