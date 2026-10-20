# Result requirements and execution evidence

The external coordinator remains responsible for planning and semantic review. Runtime checks declared delivery requirements before committing a task as successful. A completed adapter boundary can therefore produce a failed task with a retained partial result.

The browser uses the host's `execution_state` for message status and Agent indicators. A result that misses delivery requirements retains its feedback without marking the Agent as broken. Connection and timeout errors remain execution failures.

Built-in model Agents reject empty text, output-limit endings and refusals as successful text delivery. Their result metadata preserves the model's finish reason and reported usage. Plain Markdown remains valid; headings and presentation markers are not completion criteria. Artifact-only delivery can succeed without prose.

## Declaring delivery requirements

Use the same `result_policy` on a task, instance batch, workflow Agent node or map node. Its default `kind=auto` preserves ordinary adapter outputs; built-in model results also undergo the checks described above.

```python
await client.tasks.submit({
    "session_id": SESSION_ID,
    "thread_id": THREAD_ID,
    "agent_id": AGENT_ID,
    "idempotency_key": REQUEST_KEY,
    "payload": {"input": "Check the selected sections and return the findings."},
    "result_policy": {
        "kind": "structured",
        "output_schema": {
            "type": "object",
            "required": ["findings", "covered_items"],
            "properties": {"findings": {"type": "array"}},
        },
        "required_items": ["section-a", "section-b"],
    },
})
```

`kind` supports `auto`, `text`, `structured` and `artifacts`. `output_schema` validates the output object using JSON Schema 2020-12 and permits only local schema references. For a built-in model Agent, an explicitly requested structured result is decoded from its final JSON object. This does not trigger a formatting repair or another model call.

For native model Agents, that explicit final-output contract replaces the usual Markdown presentation guidance. SDK model nodes may still produce intermediate text; the task's final structured result is checked at its completion boundary.

`required_items` checks identifiers in the output's `covered_items` list. This proves declared coverage, not that the underlying review was correct. `required_artifacts` checks delivered artifact names; `min_artifacts` sets a minimum count. Existing project, session and artifact provenance checks still apply.

Read `tasks.get` to inspect a failed task's retained result. Its `metadata.acceptance` records performed checks, specific failures and `semantic_review=UNVERIFIED`. `tasks.result` continues to raise for failed tasks. A new task must use a new business intent; replaying the same request key does not repeat work.

## Inspecting execution

`client.tasks.trace(task_id, limit=50, cursor=None)` returns bounded model stage records, run timings, initial queue time and a bounded tool-call overview. The HTTP route is `GET /api/tasks/{task_id}/trace`; compact MCP uses `platform_call(area="tasks", action="trace", params={...})` after discovering that action when needed.

Model stages record request start, response-header latency, first-event and first-content latency, reported usage, end reason and classified errors. These observations do not establish whether a network fault belongs to the host, proxy or model provider. Prompts, provider response bodies and credentials are excluded from traces.

Managed process callbacks and execution failures preserve safe `RuntimeFault` codes, retryability and details across JSONL, rather than turning a model failure into a generic protocol rejection. Failure still does not create a completed boundary or prove that arbitrary process side effects stopped; the existing uncertainty and reconciliation rules remain in force.

Stage records have durable sequence cursors. Consume the next page using the returned cursor; a missing end record means no completed stage boundary was recorded. Run `commit_work_ms` measures work inside the completion transaction before its receipt is written; it is not an independent measurement of disk flush latency. Queue and model timings overlap neither by assumption nor by summing parallel calls.

The conversation offers an execution-details action. Detailed evidence is loaded on demand, and model stage updates reuse the session event stream without a separate poller.

The detail view groups records by run, model request and tool call. A tool's `parent_stage_id` identifies its recorded model request; unassociated calls remain separate. `LLMResponse.call_id` and each tool call's ID can be passed to `context.invoke_tool(..., model_call_id=..., tool_call_id=...)`. The host verifies that pair against the same task's recorded model response, including after graph replay. `context_read` records include bytes and duration, without the source body. Read a single full tool receipt through `workbench.calls(task_id, tool_call_id=...)` when needed.

The host reuses bounded HTTP connections while authentication and timeouts remain request-scoped. Provider response cookies are not shared between Agents. A received terminal SSE marker permits a short, bounded stream drain so a normally closed response can return its connection to the pool; an open transport tail does not keep a completed model response waiting indefinitely. Host shutdown closes the model clients after managed executions settle.

## Context management

The model request is a projection of retained evidence, not the complete conversation store. Agent settings default to an estimated input budget of 48,000 tokens. Set `context_window_tokens` when the provider's limit is known; the platform also reserves the configured output limit and a safety margin. A task's optional `context_budget_tokens` can narrow its Agent's budget. Estimates include tool definitions and an approximate image allowance; provider-reported usage remains authoritative.

Under pressure, oversized tool results become bounded excerpts with immutable source references. The platform retains current instructions and complete tool-call/result groups, then summarizes older work only when necessary. Working memory records its source hash and retained-history reference. Later summaries use the prior memory plus new history; stored history segments link to previous segments without duplicating the prefix. An incomplete or ineffective summary fails explicitly instead of starting a formatting-repair loop.

The summary's generation allowance is separate from its final-text target: providers may count reasoning tokens toward their output limit. One summary request remains capped by the Agent's output setting and 4,096 tokens, and is included in any continuation budget. The retained memory must still reduce history and fit the projected input budget.

Large dependency, collaboration and resume payloads use per-item previews and source references in built-in model Agents. Original SDK inputs and results remain available. Custom graphs and process Agents can use `context.project_context(value)` for the same projection and `context.read_context(reference, offset=0, limit=8192)` for bounded evidence reads. `context.list_tools()` includes the corresponding model-facing reader for managed tool loops. Reads may return a smaller chunk to fit the active budget.

External coordinators use `client.contexts.read(reference, project_id, offset=0, limit=8192)` or the compact MCP `contexts.read` action. Offsets are UTF-8 byte offsets: follow `next_offset`, and follow `previous_reference` when inspecting earlier history segments. References are immutable snapshots; new source content produces a new reference.

Thread-private sources require explicit `context_refs` assignment when handed to another task. Child tasks can inherit only sources available to their parent. Reads enforce project scope and current source-tool grants; references do not grant file or MCP permissions. Internal context artifacts do not appear among user deliverables.

Host-mediated `context.llm.complete()` rejects empty, truncated or refused responses by default. `allow_incomplete=True` is an explicit option for a controlled continuation loop, not permission to mark that response complete. The task completion boundary still rejects an unresolved incomplete model response. Truncated tool calls are never executed by the built-in loop.

## Conversation branches

Native chat checkpoints append new messages as immutable segments. Their compact references share prior segments, including across branches; conversation pages preserve the original message IDs. Older `chat-messages-v1` checkpoints remain readable. Complete history and model-visible projections have separate storage contracts.

Use `client.threads.fork(thread_id, checkpoint_id=..., idempotency_key=...)` to select a saved result, or supply its `message_id`. Omit both selectors to use the latest committed checkpoint. A stable key freezes the resolved source checkpoint and returns the same branch after a lost response. Changing the request while reusing that key is a conflict. A branch inherits only the conversation and evidence available at its selected checkpoint, excluding later queued inputs and later continuation runs.

For LangGraph, opt in with `fork_builder` and a persistent checkpointer. The selector returns safe business state; the adapter removes runtime handles, pending tools and stale model-call IDs. Only completed graph checkpoints can branch. State is seeded through the existing checkpointer without running nodes; interrupted or pending graphs must first be resolved. Declare `supports_fork` only when that behavior is implemented.

## Bounded delivery continuation

An external coordinator or the conversation's “continue refining” action may explicitly call `client.tasks.continue_result(task_id, idempotency_key, content="")`. This repairs a failed standalone delivery with retained output and a committed checkpoint, while preserving its task identity, request hash and tool keys. The thread must not have moved on, and unresolved external actions must first be reconciled. Saved workflow runs keep their finalized state; submit a separate repair task for those results.

The optional `continuation_policy` freezes maximum runs, model calls, output tokens and elapsed time when continuation starts. Defaults are two runs, four model calls, 8,192 output tokens and 120 seconds. All hosted model calls, including summaries and tool followups, consume that budget. Each request reserves its bounded output allowance; reported completion usage refunds unused tokens. Missing usage retains the full reservation. This is an execution limit, not a monetary bill or an exact input-token count.

Continuation can reuse successful tool receipts, read evidence and repair managed workspace artifacts. New external actions, collaboration messages and child dispatches are blocked. Output-limit repairs append to retained text with bounded overlap handling; schema, coverage and artifact repairs replace the prior delivery. Every completed boundary undergoes the same delivery checks. No automatic retry loop or semantic judge is introduced; substantive review remains the external coordinator's responsibility.
