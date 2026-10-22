CREATE TABLE mcp_resource_policies (
    server_id TEXT PRIMARY KEY REFERENCES mcp_servers(id) ON DELETE CASCADE,
    policy_json TEXT NOT NULL
);
CREATE TABLE mcp_call_resources (
    call_id TEXT PRIMARY KEY REFERENCES mcp_calls(id),
    connection_owner TEXT NOT NULL,
    quantities_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('HELD','ISOLATED','RELEASED')),
    evidence TEXT
);
