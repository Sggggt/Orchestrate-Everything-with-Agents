# API and transports

The local gateway binds to loopback. Requests use `Authorization: Bearer <client-token>`; the browser uses the same workspace credential. Tokens and connection metadata are written into the external data directory. Avoid logging authorization headers or embedding them in URLs.

## RPC

`POST /api/rpc` accepts:

```json
{"method": "projects.list", "params": {}}
```

Responses contain `result` or a structured `error` with a safe message and code. `GET /api/protocol` describes current methods, parameter names, models and capabilities. Namespaced methods cover projects, Agents, sessions, threads, tasks, messages, artifacts, resources, events, workflows and MCP. Use this catalog when developing a client; a compact MCP consumer should request only one area's guide.

Python `PlatformClient.call()` and CLI `oea request METHOD --params-file PATH` call this same gateway. Parameter files and saved outputs belong outside source repositories. `oea schema --output PATH` exports model schemas without requiring a running host.

## Browser REST surface

| Route group | Purpose |
| --- | --- |
| `/api/protocol`, `/api/config` | Discovery and runtime configuration |
| `/api/projects` | Project folders, membership, archive and recoverable deletion |
| `/api/sessions`, `/api/threads` | Session lifecycle, conversations and forks |
| `/api/agents` | Agent registration, model tests, profiles, skills and bindings |
| `/api/tasks` | Submission, state, results, cancel, retry, resume and reconciliation |
| `/api/messages` | Collaboration messages and receipts |
| `/api/artifacts` | File upload, metadata and checked download |
| `/api/workflows` | Saved definitions, validation, runs and recycle bin |
| `/api/mcp` | Upstream services, catalogs, calls and client configuration |
| `/api/events` | Server-sent runtime events |

Exact REST route definitions are maintained in [web/app.py](../src/ma_runtime/web/app.py). SDK clients should prefer RPC rather than depending on browser-specific projections. Legacy workflow requests carrying a `session_id` create one-off task dependencies; saved project graphs use `project_id`.

## Events and output

Persistent events have monotonic SQLite sequence numbers; SSE IDs support replay and client deduplication. Filters can create sequence gaps. Transient model token events do not advance the durable cursor. Reconnect uses a fresh snapshot and durable results; overloaded streams request a resync. The browser projects Markdown summaries while Agent peers and SDK callers can retrieve complete content.

## STDIO and MCP

`oea stdio` serves JSON-RPC 2.0 over UTF-8 JSONL. Initialize first to negotiate the protocol version, then call the discovered methods. stdout is protocol-only. Request IDs identify transport messages; business idempotency keys identify operations.

`oea mcp --connect --compact` exposes the two guided tools documented in [MCP](mcp.md). Without `--connect`, the bridge owns a standalone runtime and must use its own external state directory.

Managed Agent subprocesses use a separate JSONL contract: describe, execute, cancel, probe and run-bound reverse requests. Events carry a run ID and increasing local sequence. Committed boundaries are deduplicated by run, sequence and content hash. See the [Agent SDK](sdk.md).

Model reverse calls use the run's configured provider; they cannot choose a different endpoint, model or key through request parameters. Tool access is checked at invocation. Size limits apply to frames, messages, events and artifacts; JSONL artifact uploads are capped at 512 KiB per frame.
