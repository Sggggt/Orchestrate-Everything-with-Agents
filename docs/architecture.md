# Architecture

OEA is a personal, local, single-host Agent platform. The main Agent understands the goal, plans, delegates and verifies. LangGraph is the only orchestration engine. The capability kernel performs model, tool, context, communication and artifact work; the host owns authorization, budgets, persistence, lifecycle and recovery.

The public `oea` package provides the SDK and CLI. HTTP, control MCP and SDK share one application service and operation registry. Each [code module](modules.md) has a matching interface directory under `docs/`.

LangGraph owns node positions, conditions, parallel joins, interrupts, subgraphs and graph checkpoints. OEA owns bounded context projections, original evidence, action receipts, messages and accepted configuration snapshots. Graph state contains IDs and evidence references rather than full transcripts or secrets.

Independent instances run directly in parallel. One instance has one mutable context writer. Cost budgets cover root and child work. SQLite stores application facts, while the native SQLite checkpointer stores graph progress. Recovery reads original action receipts before continuing; a disconnected caller does not prove an external action stopped.

Runtime state, secrets and working directories live outside Git and synchronized folders. A data directory has one host owner. `PlatformClient` and the MCP bridge connect to that host. Importing or declaring an Agent does not start a host, model, MCP session or background worker. Explicit context managers close owned resources.

Agents and process programs are trusted local code with host user permissions. Tool authorization, project ownership, path boundaries and secret references are enforced at service boundaries. Skills, messages and tool output are evidence, never authority. See [security](../SECURITY.md) and [migration](migration/README.md).
