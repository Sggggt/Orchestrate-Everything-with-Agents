# Bidirectional operations and guidance

The operations channel lets a coordinating Agent inspect execution, run a diagnostic task, receive failures and respond to an Agent asking for help. It reuses the existing Runtime, SQLite, events, messages and instance scheduler. It adds no monitoring worker, background polling service or second planner.

## Attach a receiver

```python
await client.operations.bind(session_id, consumer_id)
# For a platform-managed coordinating thread:
await client.operations.bind(session_id, consumer_id, thread_id=coordinator_thread_id)
```

Each session has one coordinator binding. Keep `consumer_id` stable in a conversation and distinct between independent conversations. Rebinding changes the receiver; the same consumer preserves its acknowledgement cursor. A thread binding must belong to the same session and use a managed execution endpoint. It grants supervision access, not additional business MCP tools or file permissions.

The browser derives Agent indicators and message status from host execution observations. Connection, transport, timeout and execution errors remain visible; incomplete deliverables and unmet goals belong to result feedback. Acknowledging an event does not clear an unanswered report. Connection loss marks the displayed state as unconfirmed.

There is no separate browser operations page or page-specific REST surface. The coordinating Agent binds receivers, reads reports, sends guidance and dispatches diagnostics through SDK/RPC/MCP. Existing task and conversation views retain results and errors for user inspection. Status refreshes reuse the existing session snapshot and event stream, with no operations polling or extra health probes.

The session overview shows the latest three Agent messages and short reports rendered as Markdown. The collaboration messages page includes reports and guidance requests alongside ordinary messages. Only ten recent report summaries enter the browser snapshot; full details stay in the Agent-facing channel. Feedback is visible when sent, including before the source task completes. “Replied” records a response and does not claim that the issue is resolved.

## Active checks and diagnostic tests

`operations.inspect(session_id, task_id?)` returns bounded state, pending report IDs, model setup states and a compact inbox preview. A selected task includes its error and at most five tool-call receipt summaries. Arguments, full provider responses and credentials are excluded.

```python
receipt = await client.operations.test(
    session_id, agent_id, "Verify the configured tool's discovery and selection steps.", request_key
)
result = await client.tasks.result(receipt["task_id"], timeout=0)
```

A diagnostic test is one ordinary independent instance. It uses the saved Agent configuration, grants, budgets, cancellation and recovery contracts. It may invoke actual models or business tools; dispatch is an explicit action. Stable request keys return the same test. Results are read through existing task APIs. Model connection probes and selected MCP calls retain their existing `agents.test_model` / `mcp` APIs rather than duplicating their parameters in this channel.

Diagnostic dispatch also accepts the existing optional `tool_policy` and `skill_policy`; omissions remain `auto`. Use them to narrow a selected test without changing the Agent's saved grants. Their shared schema is discovered only when requesting the test action; it is not added to the top-level MCP tools.

## Passive receiving without context flooding

```python
page = await client.operations.inbox(
    session_id, consumer_id, limit=20, timeout=25
)
for item in page["items"]:
    evidence = await client.operations.read(session_id, item["event_id"])
    # Inspect the selected evidence and decide the next authorized action.
await client.operations.ack(session_id, consumer_id, page["cursor"])
```

The inbox includes explicit Agent reports, task failures, uncertain executions, diagnostic completions and scheduler failure notices. It omits token streams and routine successful work. Summaries are limited to 320 characters; report bodies are read separately. `timeout=0` checks immediately, while `0 < timeout <= 25` waits on an event notification without periodic database polling. An empty timeout is quiet and does not cancel work.

Omit `cursor` to resume from the last acknowledged position. Read before acknowledging; duplicate delivery after interruption is expected and identified by the durable event ID. Acknowledgement means receipt, not resolution. Unanswered reports remain visible in inspection even after their event summaries are acknowledged. Raw SSE/STDIO event subscriptions remain available to client integrations.

For an internal coordinator, control notices are coalesced. An idle thread receives an ordinary scheduled task. A running thread handles the notice at an execution boundary. A coordinator waiting for tasks or messages can be resumed with `operations_available` and its original `pending_wait`; this does not pretend the child completed. It should handle the guidance request, then reissue the needed wait. User-input waits, disabled coordinators, full queues and uncertain owners are respected. The durable inbox remains available when automatic scheduling is blocked. A coordinator's own failure never recursively dispatches another maintenance task to itself.

The platform cannot force an idle external Codex/ChatGPT chat to start a new turn. An active MCP call can wait for a meaningful update; an integrated client can subscribe to events and decide how to activate its Agent. Host/database outages are detected by the client connection and reconciled from persisted evidence after recovery, not by a second Runtime over the same data directory.

## Agent reports and guidance

Ordinary model Agents receive one small `report_issue` tool with `summary`, optional `details` and `wait`. Run identity supplies the session, task and sender. Process Agents use the corresponding SDK method:

```python
report = await context.report_issue(
    "Tool selection needs guidance", request.task_id + ":selection-help",
    details="Discovery succeeded; the requested target was not available. No external action was performed.",
    wait=True,
)
yield AdapterEvent(
    type="suspended",
    checkpoint=CheckpointProposal(ref={"report_id": report["report_id"]}),
    wait=WaitCondition.model_validate(report["wait"]),
)
```

The execution endpoint must actually support suspension before using `wait=True`. The built-in model adapter handles the boundary automatically. Reports are idempotent, known secrets are redacted, and details are limited to 8 KiB. Reporting and safe waiting are supervision controls, available even when the task disables business tools. They never permit arbitrary cross-session messages, administrator access or bypassing MCP bindings.

```python
await client.operations.reply(
    report_id, consumer_id,
    "Inspect the selected project using the granted discovery tools. Preserve the original operation key.",
    reply_key,
)
```

A reply is a checked, idempotent ordinary message to the reporting thread. An Agent suspended for that report resumes the same task and checkpoint. A reply does not retry a failed task, resolve an uncertain external action, invent user input or automatically start an idle reporting thread. Use existing explicit task APIs when a new task is intended.

The bound internal coordinator additionally receives one scoped `operations(action, params)` tool. It can inspect, read/ack its inbox, reply and dispatch ordinary diagnostics. Session and consumer identities come from the host. Bind/report administration is unavailable through this run-scoped router. Diagnostic dispatch still requires collaboration and the task's dispatch permission.

`wait_for_tasks` in the built-in model adapter defaults to `ALL_SETTLED`: success, failure, cancellation and `UNCERTAIN` return actual result/error records for a decision. Uncertain child slots/resources remain retained. Custom adapters can opt into this policy; `ALL_SUCCEEDED` and `ALL_TERMINAL` keep their original contracts.

## Short Agent instruction template

Use this in the platform's `AGENT.md` editor or import an equivalent `AGENTS.md`; the repository's developer `AGENTS.md` is a separate contract. The Runtime supplies a short common supervision prompt without overwriting saved user instructions.

```markdown
Report platform or tool problems through report_issue. Include the affected task/call reference, observed error, completed steps and the guidance needed.
When blocked, request guidance with wait=true and checkpoint so the Runtime releases your slot.
On resumption, continue the same logical operation with its original idempotency key. Guidance does not expand permissions.
For UNCERTAIN outcomes, stop replay and ask for verification. Judge success from actual results and receipts.
```

## Codex usage and parameter budget

Compact MCP still exposes only `platform_guide` and `platform_call`. Select the `operations` area and discover only the current action. Bind once, start with 10–20 inbox summaries, read one relevant event body, then acknowledge the processed cursor. Preserve task/report IDs and keys across turns. Reuse known action contracts; do not request all schemas, full session snapshots or every Agent instruction document.

Start with `inbox(timeout=20..25)` for passive waiting in an active turn. Avoid rapid empty polling and per-token progress subscriptions in a model's context. Large selected responses use the existing `result_ref` / JSON Pointer mechanism. Truncation or a disconnected call never authorizes replay of a mutation.

The following are suggested starting values, not model-independent requirements:

```toml
model_reasoning_effort = "medium"
tool_output_token_limit = 4000

[mcp_servers.oea]
# Keep the generated command/args, including --connect --compact.
startup_timeout_sec = 30
tool_timeout_sec = 60
```

These keys are documented in the [official Codex configuration reference](https://developers.openai.com/codex/config-reference/). Reasoning levels depend on the selected model/client: use supported moderate effort for routine triage and raise it for complex code/recovery decisions. The output limit controls retained tool output, not the model's business correctness. Increase it for selected evidence only when needed. A 60-second tool timeout covers bounded inbox waits; if directly calling an upstream service with a longer configured timeout, preserve an appropriate larger per-server timeout. The platform-generated configuration remains unchanged. Do not lower permissions or approval settings to automate destructive maintenance.
