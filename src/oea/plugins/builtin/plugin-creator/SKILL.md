---
name: plugin-creator
description: Package existing MCP connections and related Skills into a portable Agent Plugin.
---

Use `plugin_options` to identify the granted MCP connections and Skills needed for the capability. Use `build_plugin` with their stable IDs and an idempotency key. Export portable configuration only: no credentials, private paths, business data or conversation history. A returned package path proves packaging, not a working remote connection. Install and test through the authorized control interface, inspect component issues, and retain the original receipt when recovering output. Never register the OEA control MCP as its own upstream service.
