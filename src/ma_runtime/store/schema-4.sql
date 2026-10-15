CREATE TABLE deleted_items(
    kind TEXT NOT NULL CHECK(kind IN ('project','session')),
    entity_id TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY(kind,entity_id)
);
CREATE INDEX deleted_items_batch ON deleted_items(batch_id);
