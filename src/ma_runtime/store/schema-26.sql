CREATE TABLE resource_recoveries (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    generation TEXT,
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(owner, idempotency_key)
);
CREATE INDEX mcp_call_resources_connection_owner ON mcp_call_resources(connection_owner, state);
CREATE TABLE run_partial_results (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    result_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
