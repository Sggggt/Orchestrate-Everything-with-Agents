CREATE TABLE budget_amendments (
    budget_id TEXT NOT NULL REFERENCES execution_budgets(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    previous_policy_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(budget_id, idempotency_key)
);
