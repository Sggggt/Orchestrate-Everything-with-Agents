CREATE TABLE operations_channels (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    consumer_id TEXT NOT NULL,
    thread_id TEXT REFERENCES threads(id),
    ack_seq INTEGER NOT NULL DEFAULT 0,
    delivered_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE operations_reports (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    task_id TEXT REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    source_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    wait_for_guidance INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id),
    reply_message_id TEXT REFERENCES messages(id),
    created_at INTEGER NOT NULL,
    UNIQUE(session_id, source_key, idempotency_key)
);
CREATE INDEX operations_reports_task ON operations_reports(task_id);
CREATE INDEX tasks_thread_created ON tasks(thread_id,created_at);
