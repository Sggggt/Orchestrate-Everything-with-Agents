-- A rebuildable presentation index; source messages and application receipts
-- remain authoritative. Reading a display item never acknowledges execution.
CREATE INDEX workflow_runs_live_project ON workflow_runs(project_id,workflow_id) WHERE ended_at IS NULL;
CREATE TABLE thread_activity_items(
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    kind TEXT NOT NULL CHECK(kind IN ('conversation','message','report')),
    source_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    read_at INTEGER,
    UNIQUE(thread_id,kind,source_id)
);
CREATE INDEX thread_activity_page ON thread_activity_items(thread_id,position);
CREATE INDEX thread_activity_source ON thread_activity_items(source_id);
CREATE TABLE thread_activity_state(
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    latest_position INTEGER NOT NULL REFERENCES thread_activity_items(position),
    unread_count INTEGER NOT NULL CHECK(unread_count >= 0)
);
CREATE TRIGGER thread_activity_insert AFTER INSERT ON thread_activity_items
BEGIN
    INSERT INTO thread_activity_state VALUES(NEW.thread_id,NEW.position,CASE WHEN NEW.read_at IS NULL THEN 1 ELSE 0 END)
    ON CONFLICT(thread_id) DO UPDATE SET latest_position=NEW.position,
        unread_count=unread_count+CASE WHEN NEW.read_at IS NULL THEN 1 ELSE 0 END;
END;
CREATE TRIGGER thread_activity_read AFTER UPDATE OF read_at ON thread_activity_items
WHEN OLD.read_at IS NULL AND NEW.read_at IS NOT NULL
BEGIN
    UPDATE thread_activity_state SET unread_count=unread_count-1 WHERE thread_id=NEW.thread_id;
END;
INSERT INTO thread_activity_items(thread_id,kind,source_id,created_at)
SELECT thread_id,kind,source_id,created_at FROM (
    SELECT thread_id,'conversation' AS kind,id AS source_id,created_at FROM conversation WHERE role='assistant'
    UNION ALL
    SELECT target_thread_id,'message',id,created_at FROM messages
    UNION ALL
    SELECT source_thread_id,'message',id,created_at FROM messages WHERE source_thread_id IS NOT NULL AND source_thread_id!=target_thread_id
    UNION ALL
    SELECT t.thread_id,'report',r.id,r.created_at FROM operations_reports r JOIN tasks t ON t.id=r.task_id
) ORDER BY created_at,source_id,thread_id;
CREATE TRIGGER conversation_activity AFTER INSERT ON conversation WHEN NEW.role='assistant'
BEGIN
    INSERT INTO thread_activity_items(thread_id,kind,source_id,created_at) VALUES(NEW.thread_id,'conversation',NEW.id,NEW.created_at);
END;
CREATE TRIGGER message_activity AFTER INSERT ON messages
BEGIN
    INSERT INTO thread_activity_items(thread_id,kind,source_id,created_at) VALUES(NEW.target_thread_id,'message',NEW.id,NEW.created_at);
    INSERT INTO thread_activity_items(thread_id,kind,source_id,created_at)
        SELECT NEW.source_thread_id,'message',NEW.id,NEW.created_at
        WHERE NEW.source_thread_id IS NOT NULL AND NEW.source_thread_id!=NEW.target_thread_id;
END;
CREATE TRIGGER report_activity AFTER INSERT ON operations_reports WHEN NEW.task_id IS NOT NULL
BEGIN
    INSERT INTO thread_activity_items(thread_id,kind,source_id,created_at)
        SELECT thread_id,'report',NEW.id,NEW.created_at FROM tasks WHERE id=NEW.task_id;
END;
