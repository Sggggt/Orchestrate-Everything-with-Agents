# Architecture

OEA is a personal, local, single-host Agent platform. The main Agent understands the goal, plans, delegates and verifies. LangGraph is the only orchestration engine. The capability kernel performs model, tool, context, communication and artifact work; the host owns authorization, budgets, persistence, lifecycle and recovery.

The public `oea` package provides the SDK and CLI. HTTP, control MCP and SDK share one application service and operation registry. Each [code module](modules.md) has a matching interface directory under `docs/`.

[OAP](applications/README.md) adds independent application identities and explicit
Agent bindings to that same Host. Application sessions retain their saved
ownership through instance, Run, budget, context, action, artifact, event and
checkpoint records. One assembler intersects source capabilities with the
application grant at acceptance and recovery; current authorization also applies
to actual capability calls. Application work uses the same LangGraph engine and
native sandbox, with dedicated directories and no platform communication tools.
The management boundary filters application business objects before pagination
and rejects object-level reads; application configuration remains manageable.

LangGraph owns node positions, conditions, parallel joins, interrupts, subgraphs and graph checkpoints. OEA owns bounded context projections, original evidence, action receipts, messages and accepted configuration snapshots. Graph state contains IDs and evidence references rather than full transcripts or secrets.

Independent instances run directly in parallel. One instance has one mutable context writer. Cost budgets cover root and child work. SQLite stores application facts, while the native SQLite checkpointer stores graph progress. Recovery reads original action receipts before continuing; a disconnected caller does not prove an external action stopped.

Runtime state, secrets and working directories live outside Git and synchronized folders. A data directory has one host owner. `PlatformClient` and the MCP bridge connect to that host. Importing or declaring an Agent does not start a host, model, MCP session or background worker. Explicit context managers close owned resources.

Native commands, patch workers, media capture and process Agents share one effective OS policy through an on-demand Rust/MXC component. Explicit trusted mode and in-process extensions retain host-user authority. Tool authorization, project ownership, path boundaries and secret references are enforced at service boundaries. Skills, messages and tool output are evidence, never authority. See [security](../SECURITY.md) and [migration](migration/README.md).

Free-form boards and private inboxes carry collaboration without implicitly creating work. Manager roles are optional. Context uses bounded projections and incremental summaries; total usage, cache categories and currency-specific costs retain their distinct meanings.

Context selection distinguishes paired tool messages, current execution facts and output completeness. The Host supplies a scoped read-only view of existing command-session metadata: later terminal evidence can release an old running observation for compaction, while active or unknown work retains its identity and evidence references. This view does not rewrite receipts or authorize execution. It reuses existing persistence and bounded selection without a second job registry, background polling or an additional model request.
