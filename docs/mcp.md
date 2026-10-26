# MCP and Codex skills

The control MCP connects to an already running OEA 2 host. Install the `mcp` extra in its Python environment:

```sh
oea mcp
```

The normal connection needs no directory argument. It reads the saved endpoint and authentication from `~/.oea/data`, or an explicitly isolated `OEA_HOME`. The server only connects to the existing host; start `oea serve` first. A Codex connection uses command `oea` and arguments `["mcp"]`. Calls create OEA-owned records in that host, not a new database for each Codex chat.

It exposes `platform_guide(consumer_id, area, action)` and `platform_call(consumer_id, operation, params)`. Discover the current action, then execute the exact shared operation. SDK, HTTP and MCP use the same registry, authorization and receipts. Read the [interface module](interfaces/README.md) for the maintained transport contract.

Keep a stable consumer identity within one conversation. Independent conversations use separate identities. Lists return bounded pages. Large results return `result_ref`; `results.read` reads the selected JSON Pointer with its original project and consumer ownership. Reading a result never repeats the mutation that created it.

The wheel includes the supplied [Codex skill](../integrations/codex/orchestrate-everything-with-agents/SKILL.md). Export it to a new directory with `oea export-skill --output EXTERNAL_SKILL_DIRECTORY`, then place that directory under the client's skill directory. Export refuses an existing destination, preserving local instructions. Its router loads one current workflow. Local paths, connection names and object IDs belong in your installed local configuration.

Business MCP services are separate from platform control. Save a service with named secret references, discover its tools, then bind selected upstream names in `tools.mcp`. Discovery does not execute business work. Current project, service, Agent and Run grants are checked at invocation and replay. Consumer-scoped sessions are the default; explicit shared sessions share upstream conversational state.

For a controller's explicit tool trial use `mcp.call` and retain its original key. Read `mcp.call_result`; request cancellation with `mcp.cancel_call`. An unknown external outcome requires evidence through `mcp.confirm_call`; confirmation does not resend the invocation. See the [SDK](sdk.md) and [MCP module](mcp/README.md).
