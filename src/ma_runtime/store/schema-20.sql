CREATE TABLE resource_capacities (
    key TEXT PRIMARY KEY,
    config_json TEXT NOT NULL
);
CREATE TABLE resource_leases (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('QUEUED','GRANTED','ISOLATED','RELEASED')),
    created_at INTEGER NOT NULL,
    granted_at INTEGER,
    expires_at INTEGER,
    ended_at INTEGER,
    evidence TEXT,
    UNIQUE(owner,scope,idempotency_key)
);
CREATE INDEX resource_leases_state ON resource_leases(state,created_at);
