CREATE TABLE chat_segments (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES chat_segments(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    thread_id TEXT NOT NULL REFERENCES threads(id),
    messages_json TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE chat_checkpoint_heads (
    checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
    head_id TEXT REFERENCES chat_segments(id)
);
CREATE TABLE checkpoint_windows (
    checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
    user_seq INTEGER NOT NULL,
    message_seq INTEGER NOT NULL
);
CREATE TABLE thread_branches (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    parent_thread_id TEXT NOT NULL REFERENCES threads(id),
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
    message_seq INTEGER NOT NULL,
    user_seq INTEGER NOT NULL
);
CREATE TABLE thread_fork_receipts (
    source_thread_id TEXT NOT NULL REFERENCES threads(id),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
    completed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(source_thread_id,idempotency_key)
);
CREATE INDEX conversation_run_seq ON conversation(run_id,seq);
CREATE INDEX conversation_task_role_seq ON conversation(task_id,role,seq);
CREATE INDEX checkpoints_run ON checkpoints(created_by_run_id);
CREATE INDEX thread_fork_target ON thread_fork_receipts(thread_id);
