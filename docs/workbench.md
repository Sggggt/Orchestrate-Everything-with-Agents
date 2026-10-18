# Workbench tools and local environments

The workbench adds bounded text, search, atomic revision-checked edits, commands, images and artifact delivery to the existing run-bound tool gateway. Existing Agents receive none of these capabilities automatically.

For image support, install the `workbench` extra in the host's external Python environment. From the source checkout, run `python -m pip install -c constraints.txt ".[web,mcp,workbench]"`. The `langgraph` extra adds the optional graph adapter dependencies and SQLite checkpointer; install it in the environment that runs graph Agents. Document processors and other external programs are configured separately for the selected task environment.

Start a host with `oea serve --allow-trusted-local-execution` only when local command execution is intended. Embedded applications use `Runtime.open(..., allow_trusted_local=True)`. This host grant cannot be enabled through Agent configuration, MCP, a Skill or a guidance message.

Configure an environment through `client.environments.configure` with its project ID and a `programs` mapping from aliases to explicit absolute executable paths. The environment contains nonsecret variables and optional read-only resource directories. It does not install or bundle Python, Node or an isolation engine. `prepare` and `check` return durable `task_id` receipts immediately; retrieve the result through `client.tasks.result`.

Results distinguish base readiness, task dependencies and execution readiness. The local runner manages owned process trees, timeouts and bounded separate output streams. It does **not** isolate filesystem or network access. A requested isolation backend or `network="none"` makes execution unavailable; unrelated file tools remain usable.

An Agent's optional configuration has three fields:

```json
{
  "workbench": {
    "environment_id": "documents",
    "permission_profile": "trusted-local",
    "tools": ["read_file", "search_files", "write_file", "edit_file", "exec_command", "read_image", "publish_artifact"]
  }
}
```

`read-only` and `workspace-write` control file tools. `trusted-local` also permits configured commands when the host grants execution. Commands run with host user permissions and a minimal environment; these controls are not a system sandbox. Programs use aliases and relative task paths. No shell parsing is implicit; select a configured Shell explicitly when needed.

File tools accept mapped paths below `/workspace`, `/project` and `/resources`. Each task has its own directory outside the repository; the project is read-only unless the environment explicitly grants project writes. Link traversal, hard links and unauthorized artifact sources are rejected. Replacing a file requires its latest revision; edits must match one occurrence. Program code remains trusted local code.

Environment configuration versions are immutable. A task captures one version on its first run and reuses it after suspension. Every invocation checks current authorization. Child dispatch cannot increase the parent's workbench capabilities. Task policies only narrow permissions; required policies use durable success evidence.

`read_image` verifies PNG/JPEG/WebP files and returns a session artifact reference. Model messages may contain `{"type":"oea_image","artifact_id":"ARTIFACT_ID","detail":"auto"}` blocks. The host encodes them only for the outgoing model request. Checkpoints and tool receipts keep references, not Base64. Models must support image inputs.

Use `client.workbench.inspect` to see an Agent grant or task snapshot. `calls(task_id)` lists receipt summaries; pass `tool_call_id` to read one full result. Failed commands return their exit code and bounded stdout/stderr. Larger logs are artifacts. UNCERTAIN actions retain resources until verified; reconciliation never replays a command. Existing operations reports and guidance provide supervision.

For LangGraph, see the [managed graph reference](../integrations/codex/orchestrate-everything-with-agents/references/langgraph.md). Managed graphs receive ephemeral RunContext through GraphContext; old graphs preserve their capabilities. Persistent graphs use a separate SQLite checkpointer in external state, owned by the same host.
