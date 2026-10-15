# MCP and Codex skills

The MCP bridge connects ChatGPT, Codex or another MCP client to the same host used by the browser. A client must support the chosen MCP transport and local process execution. Product-specific client availability may vary.

## Connect a running host

```sh
oea mcp --connect --compact --data-dir /absolute/path/to/state
```

The bridge reads the host's local connection metadata and token. It does not start another scheduler. Omit `--connect` only when intentionally running a standalone bridge with its own state directory.

In the frontend, open the MCP integration guide to copy the generated Codex configuration. The SDK provides it with `await client.mcp.codex_config()`. The generated server ID is `oea`, uses the current Python executable, and exposes only:

- `platform_guide(area, action?)`: a small index or one operation's parameters.
- `platform_call(area, action, params)`: one scoped operation.

Keep an existing `multi_agent` server registration when upgrading if clients already depend on that ID; the tool names and operations remain compatible. Install the new package in the executable's environment before switching its command to `python -m oea`.

Discover one area, then one action. For example, inspect `workflows.create`, supply its parameters, and verify the returned workflow ID. Areas cover projects, sessions, threads, tasks, messages, artifacts, workflows, Agents, profiles, shared MCP services and runtime control. Large responses return a session-local reference; use `control.read_result` with a JSON Pointer instead of rerunning a mutation. Check both the outer `ok` flag and any upstream tool `isError`.

Resources `oea://tool-index`, `oea://projects` and `oea://output-contract` provide small discovery and presentation contracts. The older `multi-agent://` URIs remain aliases. Standard mode without `--compact` exposes named tools and protocol discovery.

## Install the supplied Codex skill

Copy [orchestrate-everything-with-agents](../integrations/codex/orchestrate-everything-with-agents/SKILL.md)'s containing directory into `$CODEX_HOME/skills` (normally `~/.codex/skills`). Invoke `$orchestrate-everything-with-agents`, or let Codex select it for an established platform task.

The entry point routes to one workflow at a time: Agent development, configuration, project management, graph orchestration, tasks, messages, artifacts, MCP integration or maintenance. Its [local instance reference](../integrations/codex/orchestrate-everything-with-agents/references/local-instance.md) is a template. Fill your installed copy with local paths; keep those paths out of the public repository. If retaining a legacy MCP ID, change the installed skill's dependency metadata to that ID.

## Share upstream tools

Register STDIO, Streamable HTTP or SSE services through the frontend or `client.mcp.configure()`. Credentials go through `secret_values`, not command arguments or URLs. Bind the required tools to an Agent with `mcp_bindings`. Skills and instructions guide behavior; the runtime's bindings enforce tool access.

Use a stable, distinct `consumer_id` per conversation. Consumer-scoped connections isolate stateful upstream sessions; shared connections intentionally share them. Discover the tool catalog first, then request the selected tool's schema. Do not import this platform's own bridge back into itself.

The platform does not widen an external service's authorization. Unknown tool outcomes require reconciliation; the bridge never makes blind retries safe.
