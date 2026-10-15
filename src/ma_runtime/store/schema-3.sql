CREATE TABLE projects(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    local_folder TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'OPEN',
    creation_key TEXT UNIQUE NOT NULL,
    request_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
INSERT INTO projects(id,name,creation_key,request_hash,created_at,updated_at)
VALUES('default','未分类项目','migration:3','legacy',0,0);
ALTER TABLE sessions ADD COLUMN project_id TEXT REFERENCES projects(id);
UPDATE sessions SET project_id='default' WHERE project_id IS NULL;
CREATE TRIGGER sessions_project_required_insert BEFORE INSERT ON sessions
WHEN NEW.project_id IS NULL BEGIN SELECT RAISE(ABORT,'project_id is required'); END;
CREATE TRIGGER sessions_project_required_update BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id IS NULL BEGIN SELECT RAISE(ABORT,'project_id is required'); END;
CREATE INDEX sessions_project ON sessions(project_id,created_at);
UPDATE agent_configs SET config_json=json_set(config_json,'$.project_id','default');
ALTER TABLE conversation ADD COLUMN summary_markdown TEXT NOT NULL DEFAULT '';
