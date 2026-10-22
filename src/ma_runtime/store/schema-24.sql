CREATE TABLE model_observations (
    stage_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    tools_requested INTEGER NOT NULL,
    tool_calls INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX model_observations_agent_config ON model_observations(agent_id,fingerprint,created_at DESC);
