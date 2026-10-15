CREATE TABLE workflow_deletions(
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
    deleted_at INTEGER NOT NULL
);
