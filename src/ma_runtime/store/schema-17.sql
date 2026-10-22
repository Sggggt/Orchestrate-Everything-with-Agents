CREATE TABLE context_merges (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    parent_id TEXT REFERENCES context_merges(id),
    target_revision INTEGER NOT NULL,
    target_checkpoint_id TEXT REFERENCES checkpoints(id),
    sources_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    resolutions_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(thread_id,idempotency_key)
);
CREATE TABLE thread_context_heads (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    merge_id TEXT NOT NULL REFERENCES context_merges(id)
);
CREATE TABLE checkpoint_context_heads (
    checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
    merge_id TEXT NOT NULL REFERENCES context_merges(id)
);
CREATE TABLE task_context_heads (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    merge_id TEXT NOT NULL REFERENCES context_merges(id)
);
CREATE TABLE task_artifact_access (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    PRIMARY KEY(task_id,artifact_id)
);
CREATE TRIGGER context_merges_immutable BEFORE UPDATE ON context_merges BEGIN
    SELECT RAISE(ABORT,'context merge commits are immutable');
END;
