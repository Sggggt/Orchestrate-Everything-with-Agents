# Skills and execution policies

The platform registers reusable Skills once. Agents link them with `skill_bindings`, independently of their MCP grants. The **Skill integration** page provides a directory, Markdown/source previews, reference files, refresh, enable/disable and removal. Agent settings provide association checkboxes.

## Standard Skill packages

Use the [Agent Skills format](https://agentskills.io/specification): a directory named after its Skill, containing `SKILL.md` with YAML frontmatter and Markdown instructions. Optional `references/`, `scripts/` and `assets/` directories remain part of the source package.

```markdown
---
name: review-results
description: Review task results and inspect evidence. Use when verifying completed work.
license: MIT
metadata:
  version: "1"
---

# Review results

Inspect actual receipts before reporting success.
```

Names contain lowercase letters, numbers and single hyphens, match the directory name and contain at most 64 characters. A nonempty description is required and limited to 1024 characters. Optional `license`, `compatibility`, `metadata` and `allowed-tools` are preserved; `metadata` maps strings to strings. `allowed-tools` is descriptive and does not grant Runtime permissions.

```python
skill = await client.skills.register_directory(skill_directory)
await client.agents.update(agent_id, skill_bindings=[{"skill_id": skill["id"]}])
capabilities = await client.agents.capabilities(agent_id)
```

Both project-local and external absolute directories are accepted. A source checkout may keep local registrations under root `skills/` and `mcp/`; both are ignored by Git and excluded from distribution packages. Runtime databases, secrets and generated outputs still belong outside source repositories.

Registration and explicit refresh read a version snapshot. New executions use the registered version; active executions keep their captured instructions. Editing files alone does not update a registration. Disabling a Skill prevents new reads, including in active runs. Unlink Agents before removing a registration; source files are preserved.

`SKILL.md` is limited to 64 KiB. Up to 32 UTF-8 references totaling 256 KiB are retained; registration inspects at most 512 package entries. Combined linked and embedded Skill snapshots are limited to 512 KiB per Agent. Binary or individually oversized assets remain in the source package without entering model context. Reads use registered relative paths; traversal and linked resources are rejected. The loader never executes scripts. An action requires an independently authorized tool or trusted Agent implementation.

Legacy per-Agent embedded `skills` and profile APIs remain available. Linked and embedded Skill IDs must be unique within an Agent. New integrations should use shared registrations.

## Capability discovery

`agents.capabilities(agent_id)` returns Skill descriptions, MCP bindings and platform tool names without document bodies or model credentials. Read one tool schema only when needed. Administrator previews use `skills.read`; executing Agents use `context.read_skill`.

Compact MCP provides a `skills` area: `list`, `get`, `register_directory`, `refresh`, `set_enabled`, `read` and `delete`. RPC and SDK expose `skills.*` and `client.skills`. Agent associations use `agents.patch` / `client.agents.update`.

## Task policies

Tasks, thread turns, instance batches and Agent/map workflow nodes accept `tool_policy` and `skill_policy`. Omitted policies default to `{"mode": "auto"}`.

| Field | Behavior |
| --- | --- |
| `mode: auto` | Agent chooses within grants and the allowed set. |
| `mode: none` | Disable new uses of this capability class. |
| `mode: required` | Require at least one successful tool call or Skill body load. |
| `allowed` | Optional subset of granted capabilities; an empty list permits none. |
| `required` | Exact identifiers requiring evidence; also works with `mode: auto`. |

Tools use stable IDs such as `mcp:documents:read` or platform names `list_threads`, `send_message`, `submit_task` and `spawn_instances`. Process Agents additionally expose `put_file` and `put_bytes`. Skills use their registered IDs. `read_skill` follows the Skill policy. Lifecycle waits remain available so restricted Agents can suspend and release their slots.

```python
receipt = await client.threads.turn(
    thread_id, "Inspect the document and review the result.", request_key,
    tool_policy={
        "allowed": ["mcp:documents:search", "mcp:documents:read"],
        "required": ["mcp:documents:read"],
    },
    skill_policy={"required": ["review-results"]},
)
```

Policies only narrow grants. Unlinked, disabled or invalid selections fail validation. Explicit required Skills are loaded before execution and supplied to the model. Other Skills are progressively loaded. The built-in model adapter applies provider tool-choice controls while required actions remain pending, then restores automatic selection. Unsupported controls produce errors rather than silently dropping restrictions.

Runtime checks cover managed process Agents too. Their custom loops consume the request policies, discover via `context.list_tools()` and execute through `context.invoke_tool()`. `context.llm.complete` supplies required Skill instructions; custom adapters still implement their own tool loop. Local processes remain trusted programs rather than operating-system sandboxes. Failed or interrupted processes with partial tool mediation retain the existing `UNCERTAIN` outcome contract; policy enforcement does not imply that arbitrary external effects are known.

Successful calls and Skill body loads produce durable task-scoped evidence, surviving suspension and restart. Reference reads alone do not satisfy required body loads. Upstream `isError` results do not count as success. A `completed` boundary lacking evidence fails with `EXECUTION_REQUIREMENT_UNMET`; model prose cannot replace receipts. Evidence proves execution, while business correctness still requires verification. `none` limits new operations and does not erase existing conversation history. Preserve task IDs and idempotency keys when recovering accepted actions.

## MCP source directories

`client.mcp.register_directory(directory)` / compact `mcp.register_directory` read a selected directory containing `mcp.json`:

```json
{
  "id": "documents",
  "name": "Documents",
  "transport": "stdio",
  "command": "python",
  "args": ["-B", "server.py"]
}
```

The manifest uses `MCPServerConfig` fields. Relative STDIO working directories resolve against the selected directory; omission uses that directory. Registration does not execute or connect the server. Re-register the same directory to refresh; conflicting IDs from other sources are rejected. Credentials are configured separately in encrypted storage; manifest import rejects `env`, `headers` and `secret_values`.
