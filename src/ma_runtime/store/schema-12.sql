CREATE TABLE context_sources (
    reference TEXT PRIMARY KEY REFERENCES artifacts(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    thread_id TEXT NOT NULL REFERENCES threads(id),
    source_hash TEXT NOT NULL,
    origin TEXT NOT NULL,
    parent_reference TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(project_id,source_hash,origin,parent_reference)
);
CREATE TABLE context_access (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    reference TEXT NOT NULL REFERENCES context_sources(reference),
    PRIMARY KEY(task_id,reference)
);
CREATE TABLE context_memories (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    source_hash TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    reference TEXT NOT NULL REFERENCES context_sources(reference),
    archive_ref TEXT NOT NULL REFERENCES context_sources(reference),
    updated_at INTEGER NOT NULL
);
