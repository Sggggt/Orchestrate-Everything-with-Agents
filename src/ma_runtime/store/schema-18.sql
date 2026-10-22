CREATE TABLE thread_workdirs (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    kind TEXT NOT NULL CHECK(kind IN ('managed','git')),
    repository TEXT,
    commit_sha TEXT,
    path TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('PREPARING','READY','UNCERTAIN')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
