CREATE TABLE execution_budgets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    policy_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    model_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id,idempotency_key)
);
CREATE TABLE task_budgets (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    budget_id TEXT NOT NULL REFERENCES execution_budgets(id) ON DELETE CASCADE,
    stalled_calls INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX task_budgets_owner ON task_budgets(budget_id);
CREATE TABLE workflow_budgets (
    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    budget_id TEXT NOT NULL REFERENCES execution_budgets(id) ON DELETE CASCADE
);
CREATE TABLE batch_budgets (
    batch_id TEXT PRIMARY KEY REFERENCES instance_batches(id) ON DELETE CASCADE,
    budget_id TEXT NOT NULL REFERENCES execution_budgets(id) ON DELETE CASCADE
);
CREATE TABLE budget_entries (
    id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL REFERENCES execution_budgets(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'RESERVED',
    created_at INTEGER NOT NULL
);
CREATE INDEX budget_entries_owner ON budget_entries(budget_id,created_at);
CREATE TABLE budget_progress (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(task_id,fingerprint)
);
CREATE TABLE budget_upgrade (id INTEGER PRIMARY KEY CHECK(id=1), applied INTEGER NOT NULL DEFAULT 0);
INSERT INTO budget_upgrade(id) VALUES(1);
