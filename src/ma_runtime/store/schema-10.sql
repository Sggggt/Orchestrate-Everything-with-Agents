CREATE TABLE environment_versions (
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(id,revision)
);
CREATE TABLE task_workbenches (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    snapshot_json TEXT NOT NULL
);
CREATE TABLE environment_jobs (
    environment_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    PRIMARY KEY(environment_id,idempotency_key)
);
