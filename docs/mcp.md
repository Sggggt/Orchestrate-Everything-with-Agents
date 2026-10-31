# MCP and Codex skills

The control MCP connects to an already running OEA 2 host. The [recommended uv tool installation](getting-started.md#quick-install) includes the `mcp` extra. Use the installed command:

```sh
oea mcp
```

The normal connection needs no directory argument. It reads the saved endpoint and authentication from `~/.oea/data`, or an explicitly isolated `OEA_HOME`. The server only connects to the existing host; start `oea serve` first. A Codex connection uses command `oea` and arguments `["mcp"]`. Calls create OEA-owned records in that host, not a new database for each Codex chat.

After `uv tool update-shell`, restart your MCP client so it sees the updated PATH. With a manually managed Python environment, a graphical client may not inherit the activated terminal's PATH. Use `(Get-Command oea).Source` in Windows PowerShell, `where oea` in Windows Command Prompt, or `command -v oea` on Linux/macOS to find the installed executable. Set that absolute executable path as the client command when needed; keep arguments `["mcp"]`. The [cross-platform setup guide](getting-started.md#mcp-client) covers command lookup. Windows convenience scripts are optional.

It exposes `platform_guide(consumer_id, area, action)` and `platform_call(consumer_id, operation, params)`. Discover the current action, then execute the exact shared operation. SDK, HTTP and MCP use the same registry, authorization and receipts. Read the [interface module](interfaces/README.md) for the maintained transport contract.

Keep a stable consumer identity within one conversation. Independent conversations use separate identities. Lists return bounded pages. Large results return `result_ref`; `results.read` reads the selected JSON Pointer with its original project and consumer ownership. Reading a result never repeats the mutation that created it.

Continue with the returned `next_offset` on the same reference and Pointer. Pages expose a fixed content version and selected-text hash; dynamic responses retain their original snapshot even while execution progresses. `limit` is 1..2048 Unicode code points, and a Pointer is at most 512 characters. To observe newer state, issue a new read-only query. An unavailable old version must not be recovered by repeating a write.

`projects.delete` is the confirmed permanent project revocation operation: exact name confirmation and a stable key revoke work and remove all owned records, unresolved usage and graph checkpoints. Former ledger or execution status does not veto deletion. Reuse the original key after an interrupted cleanup. Source files, backups and other projects remain; external effects already issued are not rolled back.

Delete active registrations with `agents.remove`, `mcp.remove`, or `skills.remove` through `platform_call`. Discover the selected action with `platform_guide`, then pass its object ID, project ID and current `expected_revision`. Frontend delete buttons use these same operations, including when source files are missing. Removal hides the registration and withdraws future use while retaining history, receipts and source files. An Agent with unfinished or unresolved work must be settled before deletion. Existing configuration references are retained for explicit repair. The control MCP continues to expose exactly two tools.

The wheel includes the supplied [Codex skill](../integrations/codex/orchestrate-everything-with-agents/SKILL.md). Export it to a new directory with `oea export-skill --output EXTERNAL_SKILL_DIRECTORY`, then place that directory under the client's skill directory. Export refuses an existing destination, preserving local instructions. Its router loads one current workflow. Local paths, connection names and object IDs belong in your installed local configuration.

Business MCP services are separate from platform control. Save a service with named secret references, discover its tools, then bind selected upstream names in `tools.mcp`. Discovery does not execute business work. Current project, service, Agent and Run grants are checked at invocation and replay. Consumer-scoped sessions are the default; explicit shared sessions share upstream conversational state.

For a controller's explicit tool trial use `mcp.call` and retain its original key. Read `mcp.call_result`; request cancellation with `mcp.cancel_call`. An unknown external outcome requires evidence through `mcp.confirm_call`; confirmation does not resend the invocation. See the [SDK](sdk.md) and [MCP module](mcp/README.md).
