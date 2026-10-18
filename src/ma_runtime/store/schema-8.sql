CREATE TABLE skill_registry (
    id TEXT PRIMARY KEY,
    directory TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    skill_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE execution_evidence (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    created_at INTEGER NOT NULL,
    PRIMARY KEY(task_id, kind, name)
);
