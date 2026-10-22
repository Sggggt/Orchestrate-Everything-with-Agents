CREATE TABLE message_delivery (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('DELIVERED','OFFERED','APPLIED','REJECTED','EXPIRED')),
    accepted_at INTEGER NOT NULL,
    delivered_at INTEGER NOT NULL,
    offered_at INTEGER,
    received_at INTEGER,
    applied_at INTEGER,
    boundary_run_id TEXT REFERENCES runs(id),
    disposition_key TEXT,
    disposition_hash TEXT,
    reason TEXT,
    ended_at INTEGER
);
INSERT INTO message_delivery(message_id,state,accepted_at,delivered_at,offered_at,applied_at,boundary_run_id)
SELECT id,CASE WHEN state IN ('OFFERED','APPLIED') THEN state ELSE 'DELIVERED' END,
    created_at,created_at,CASE WHEN state IN ('OFFERED','APPLIED') THEN updated_at END,
    CASE WHEN state='APPLIED' THEN updated_at END,CASE WHEN state='APPLIED' THEN target_run_id END
FROM messages ORDER BY rowid;
CREATE TRIGGER message_delivery_insert AFTER INSERT ON messages BEGIN
    INSERT INTO message_delivery(message_id,state,accepted_at,delivered_at)
    VALUES(NEW.id,'DELIVERED',NEW.created_at,NEW.created_at);
END;
CREATE TRIGGER message_delivery_state AFTER UPDATE OF state ON messages
WHEN NEW.state IN ('PENDING','OFFERED','APPLIED') BEGIN
    UPDATE message_delivery SET
        state=CASE WHEN NEW.state='PENDING' THEN 'DELIVERED' ELSE NEW.state END,
        offered_at=CASE WHEN NEW.state='OFFERED' THEN NEW.updated_at ELSE offered_at END,
        applied_at=CASE WHEN NEW.state='APPLIED' THEN NEW.updated_at ELSE applied_at END,
        boundary_run_id=CASE WHEN NEW.state='APPLIED' THEN NEW.target_run_id ELSE boundary_run_id END
    WHERE message_id=NEW.id AND state NOT IN ('REJECTED','EXPIRED');
END;
CREATE TABLE execution_channels (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE REFERENCES threads(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    connection_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    received_position INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK(state IN ('CONNECTED','DISCONNECTED')),
    updated_at INTEGER NOT NULL
);
