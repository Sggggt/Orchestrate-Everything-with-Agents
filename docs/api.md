# API and transports

The local gateway binds to loopback. Requests use `Authorization: Bearer <client-token>`; the browser uses the same workspace credential. Tokens and connection metadata are written into the external data directory. Avoid logging authorization headers or embedding them in URLs.

## RPC

`POST /api/rpc` accepts:

```json
{"method": "projects.list", "params": {}}
```

Responses contain `result` or a structured `error` with a safe message and code. `GET /api/protocol` describes current methods, parameter names, models and capabilities. Namespaced methods cover projects, Agents, sessions, threads, tasks, messages, artifacts, resources, events, workflows, operations and MCP. Use this catalog when developing a client; a compact MCP consumer should request only one area's guide.

Python `PlatformClient.call()` and CLI `oea request METHOD --params-file PATH` call this same gateway. Parameter files and saved outputs belong outside source repositories. `oea schema --output PATH` exports model schemas without requiring a running host.

Current SDK, CLI, MCP and browser clients select `X-OEA-View-Version: 1.1`. HTTP requests without this header use view `1.0`: resource snapshots retain their original fields, and workflow defaults remain readable by strict legacy models. A workflow using `collect_independent` or `all_settled` requires view `1.1`; legacy reads return `VIEW_VERSION_REQUIRED` instead of silently changing those policies. Unsupported versions are rejected. View selection does not change task types or request hashes. `PlatformClient(..., view_version="1.0")` explicitly selects the legacy view.

Public Python imports are organized as `oea.client`, `oea.execution`, `oea.context`, `oea.tools` and `oea.workflows`. Existing top-level `oea`, `ma_runtime`, `agent` and CLI imports remain available. Importing the remote client and execution contracts does not load or start a Runtime or require Web, MCP, LangGraph or image dependencies.

Agent model settings optionally accept `reasoning_effort`. The host passes an explicitly saved value to the provider unchanged; omission sends no parameter and preserves existing serialization and observation fingerprints. Native and process model calls use the same saved snapshot, and execution requests cannot override it. Provider support must be verified for the selected endpoint; unsupported values produce the provider's normal error, without automatic fallback or retry. Reasoning tokens remain part of reported output usage and cumulative limits. Explicit reasoning settings require view `1.1`; a legacy read or replacement that would hide or erase them returns `VIEW_VERSION_REQUIRED` before mutation. The browser exposes this option under advanced model parameters.

Provider HTTP failures retain bounded, credential-redacted `provider_code`, `provider_type`, `provider_param` and `provider_message` when supplied as JSON. Diagnostic reading is limited to 8 KiB and one second; unavailable or non-JSON bodies do not hide the original HTTP failure. Raw request bodies and authorization headers are not copied into errors.

Task reconciliation requires evidence that execution stopped and external effects were checked. A failed or cancelled resolution preserves the retained result and original error when no replacement is supplied. Run errors retain the original cause alongside reconciliation evidence. A successful resolution still checks required execution evidence and the task's declared delivery policy before releasing uncertain occupancy.

## Read-only admission facts

`admission.candidates(project_id, ...)` pages through managed Agents and applies explicit tool, Skill, model and declared execution-capability requirements. Each entry reports eligibility, exclusion reasons, unknown live availability and phase capacity. It does not select an Agent, reserve resources or create objects. Exhausted capacity means waiting; it is not a reason to start another host.

`admission.describe(agent_id)` separates configured purpose from the adapter's actual declarations and current grants. Model observations are limited to the latest 32 requests matching a settings fingerprint, with sample counts, time range, measured latency and reported tokens. The fingerprint excludes credentials. Samples include summaries and mixed workloads; no matched benchmark or price data exists, so these observations do not rank Agents by cost or speed, prove expertise or establish semantic quality. Older unversioned traces are not treated as current observations.

`admission.check(spec, budget_id=None)` reuses the task submission boundary for scope, policy, context references, work directory, dependencies and queue limits. It also reports known configuration/dependency blocks, capacity waits, cumulative budget limits and unknown provider behavior. It never creates a task, connects MCP, probes a model or defines capacity. `admissible` means no known hard blocker; `ready_now` additionally requires no wait or unknown. An identical submitted key returns the existing task receipt. Actual submission and execution recheck current state. Adapter payload semantics, the final subprocess frame and projected model context remain separate execution-time checks.

## Cumulative execution budgets

`budgets.create(session_id, policy, idempotency_key)` creates a durable budget in an open session. Supply its `budget_id` to `tasks.submit`, `threads.turn`, `instances.spawn` or `workflows.run` to share limits across those executions. Omission creates a default budget for the root task, batch or workflow. Child tasks, retries, workflow repairs, continuation requests and model summaries inherit that budget; reused successful evidence is not charged again.

`BudgetPolicy` defaults to 128 model calls, 2,000,000 input tokens, 262,144 output tokens, 1,024 tool calls and 86,400 elapsed seconds from first execution. Elapsed time includes suspension. Each model request reserves its estimated input and output ceiling atomically, and the configured output ceiling is reduced to remaining capacity. Valid provider usage settles that reservation; missing usage retains estimated values. These are usage limits, not exact monetary charges. `budgets.get`, `for_task` and paginated `entries` expose usage, remaining capacity and estimated charges.

By default, six consecutive model requests without new verified evidence stop the task with `PROGRESS_STALLED`. Reading a new source version or range and persisting a new file or artifact can advance progress; repeated evidence and progress prose do not. Administrators may name additional evidence-producing tools in `progress_tools`. Semantic quality remains a separate result check. A budget failure cannot be converted into success by catching its exception inside an adapter; available partial output and trace records are retained.

An administrator may explicitly call `budgets.amend(budget_id, policy, expected_policy_hash, idempotency_key, reason)` after reading `policy_hash` from `budgets.get`. The amendment records both policies and its reason atomically; usage, original start time, progress counters and descendant bindings remain intact. Limits cannot fall below already charged usage. Repeating the same key returns the same amendment receipt; stale policy hashes and changed parameters conflict. Execution Agents cannot amend budgets, and exhaustion does not automatically request or apply an increase. Native tool loops use this cumulative budget instead of a separate fixed round count.

For opted-in MCP tools, progress compares `content` and `structuredContent`, excluding call receipts and protocol metadata. Producers should keep volatile transport diagnostics in MCP `_meta` rather than mixing them into evidence content. This detects repeated content, not semantic usefulness; final review remains necessary.

On upgrade, the host imports historical model spans and mediated tool receipts once, retaining known lineage and provider usage. Older spans without usage keep conservative input/output reservations. Historical operations without receipts cannot be reconstructed and are not represented as measured usage.

The `environments` and `workbench` namespaces use this same RPC route. Environment preparation/checks immediately return task receipts; Agent workbench grants are part of Agent configuration. See [workbench](workbench.md) for local execution authorization and receipt inspection.

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

The browser submits one-off session dependencies through `tasks.workflow` RPC and uses `workflows.*` for saved project graphs. Task inspectors use the current browser presentation endpoints. Codex setup displays the generated compact tool allowlist, environment and connection parameters from `/api/mcp/codex-config`.

Supervision uses `operations.*` through RPC, SDK or MCP. There is no dedicated browser operations page or operations REST route. Task and run snapshots expose host-derived `execution_state` separately from task delivery acceptance. Conversation entries use their recorded run's observation. The session presentation supplies compact `attention_thread_ids` and `failed_thread_ids`; the latter includes execution errors rather than delivery failures. An indexed per-thread lookup preserves these observations beyond the bounded task history. Existing events refresh the views without another subscription or polling loop.

Task completion counts, task badges and conversation delivery labels use the host's task/acceptance outcome. A completed execution with failed delivery is shown as a delivery failure and is excluded from completed-task counts; it does not make an otherwise available Agent unavailable. Failed delivery summaries retain that distinction, including when projecting older stored conversations, without rewriting their raw results.

The browser presentation also includes `agent_reports`: at most ten recent run-sourced report summaries and response states. Report details are excluded. Each Agent card shows its latest durable message/report as safe Markdown; the collaboration messages view includes a report/guidance filter. Ordinary message payloads retain only their bounded presentation summary in this view, while peer APIs keep complete content. Both views reuse the existing event refresh path.

The presentation includes `thread_activity` and `message_activity` for per-thread unread counts and message positions. `threads.instances` pages through one session's instances, optionally filtered by Agent; `threads.activity` reads a bounded thread feed. `threads.mark_read(thread_id, positions)` marks only explicit rendered positions from that thread and is idempotent. Positions from another thread are rejected. These read receipts are independent of message delivery, application and operations guidance. Ordinary SDK snapshots retain their strict shape; the extra presentation fields are opt-in.

The browser keeps task cancellation, recovery, results and traces inside conversations instead of a standalone task page. Agent cards and sidebar entries open the same instance picker. Only durable messages change activity ordering; token streams do not create unread entries. The session SSE route also carries workflow events from the same project, so run selectors update without instance polling. Other sessions' ordinary task/message events remain outside that subscription.

Unread badges show the number of new messages inside a red rounded square and disappear at zero. Instance totals are not shown as notification badges. Sidebar status dots appear before the Agent identity, independently of unread state.

`threads.history(thread_id, checkpoint_id=..., cursor=..., limit=...)` reads native messages without executing them. Continue with the returned checkpoint ID and cursor; each page limits message count and body bytes, with a single large message retained intact. `contexts.merge(spec)` accepts `ContextMergeSpec` with a target revision, source thread revisions/checkpoints, named evidence references and SHA-256 versions. Conflicting names require a selected reference and reason. `contexts.get_merge` retrieves the durable receipt; `read_context` can read the assigned manifest and selected UTF-8 artifacts during execution.

`threads.prepare_workdir(thread_id, idempotency_key, kind="managed"|"git", commit=...)` binds a directory to an idle thread. Git mode requires an explicit commit or ref and freezes its resolved commit. `threads.workdir` inspects the binding. A failed or interrupted preparation retains its files and blocks new work until preparation is confirmed; repeat the original request to inspect and recover a matching, clean directory.

## Events and output

Persistent events have monotonic SQLite sequence numbers; SSE IDs support replay and client deduplication. Filters can create sequence gaps. Transient model token events do not advance the durable cursor. Reconnect uses a fresh snapshot and durable results; overloaded streams request a resync. The browser projects Markdown summaries while Agent peers and SDK callers can retrieve complete content.

## STDIO and MCP

`messages.delivery(message_id)` exposes accepted, delivered, offered, received and applied timestamps plus the persistent application boundary. Legacy message snapshots retain their original shape and PENDING/OFFERED/APPLIED projection; use the delivery view to detect rejection or expiration. `messages.reject` records an immutable reason and key; `messages.expire` additionally requires an elapsed deadline. An offered message can only be declined by its receiving execution, and an applied message cannot be revoked. Expiration is explicit; these actions do not add a timer service.

`channels.get(thread_id)` reports the logical channel generation and transport receipt position. Managed JSONL SDK peers negotiate `oea.channel=1.0` outside the strict Agent descriptor and execution-request bodies. `RunContext.inbox` offers a bounded page, `acknowledge_messages` confirms contiguous receipt, and `reject_message` declines an unapplied message. Application still requires `applied_message_ids` in a committed boundary. Legacy peers remain supported without claiming reconnect-generation capability.

After a committed completion or suspension boundary, messages without an application receipt remain available for a later execution. A new execution also recovers such offers left by older hosts, using the recorded boundary as evidence. Running or uncertain executions retain their offers until their outcome is resolved; delivery alone never starts an idle thread.

`RunContext.reconnect_channel(connection_id)` replaces a negotiated logical connection within the same running execution, retaining its inbox receipt position. Reuse the same connection ID after a lost response; the generation changes once. JSONL peers require the host's `reconnect` capability and receive `UNSUPPORTED_CAPABILITY` from older hosts. Delayed frames with an older generation are rejected without ending the current execution. This does not restart a process or recover a broken operating-system pipe.

`a2a.capabilities/send/get_task` provide internal mappings pinned to [A2A v1.0.1](https://github.com/a2aproject/A2A/blob/v1.0.1/specification/a2a.proto). `contextId` identifies an existing Thread. Incoming messages use `ROLE_USER`; responses use `ROLE_AGENT`. Message mode stores a notification without dispatching a model; task mode creates an ordinary task. New execution intent after a terminal result creates a new task and may refer to the previous one through `referenceTaskIds`. Uncertain execution maps to `TASK_STATE_UNSPECIFIED` with an explicit OEA extension. Artifact parts carry local references without invented download URLs. Pure converters, including AgentCard mapping for caller-supplied interface bindings, are available in `oea.a2a`. No A2A network binding or external interoperability is advertised.

`resources.configure(ResourceCapacity)` defines a named quantity capacity; `resources.preflight` checks a bounded resource combination without creating an object or reserving capacity. `resources.request(ResourceLeaseSpec)` returns a durable lease, advanced by the existing scheduler. `resources.get` reports current state and queue reasons. `resources.release_lease` cancels a queued request; a granted or isolated lease requires `confirmed_stopped=true` and actual evidence. Repeating the same request key recovers the lease; changed input conflicts. Execution capacities follow Runtime, Agent and batch settings. Model request capacities use `model:<endpoint-and-model-hash>` keys visible in `resources.inspect`.

`mcp.configure_policy(server_id, MCPResourcePolicy)` sets service call capacity, reserved connection capacity and idle connection behavior without changing `MCPServerConfig`. Persistent mode is the default. Stateless and reinitialize modes allow idle reclamation under pressure; reinitialize requires explicit connection-state-only initialization steps. These steps must be safe to repeat on a fresh connection and obey the same current and captured grants. `mcp.policy` shows actual connections, handshakes and schema fingerprints. `mcp.reconcile_capacity` requires stop evidence and releases isolated occupancy only; an uncertain call's outcome and original idempotency receipt remain unchanged.

`oea stdio` serves JSON-RPC 2.0 over UTF-8 JSONL. Initialize first to negotiate the protocol version, then call the discovered methods. stdout is protocol-only. Request IDs identify transport messages; business idempotency keys identify operations.

`oea mcp --connect --compact` exposes the two guided tools documented in [MCP](mcp.md). Without `--connect`, the bridge owns a standalone runtime and must use its own external state directory.

`mcp.catalog(server_id, cursor=None, limit=25)` reads a cached, paginated tool directory without connecting. Entries contain stable identifiers, protocol version, discovery time, schema fingerprint, byte size and an explicitly estimated token size. `mcp.tool(server_id, tool_name, expected_fingerprint=None)` returns one complete schema, including recursive definitions. A stale expected fingerprint is rejected. Local schema validity at discovery, OEA model-input feasibility, provider tool support and upstream execution are reported separately; discovery alone does not prove the latter three. Existing `mcp.tools` behavior remains available.

Managed Agent subprocesses use a separate JSONL contract: describe, execute, cancel, probe and run-bound reverse requests. Events carry a run ID and increasing local sequence. Committed boundaries are deduplicated by run, sequence and content hash. See the [Agent SDK](sdk.md).

Model reverse calls use the run's configured provider; they cannot choose a different endpoint, model or key through request parameters. Tool access is checked at invocation. Size limits apply to frames, messages, events and artifacts; JSONL artifact uploads are capped at 512 KiB per frame.

Work artifact metadata includes platform-owned `provenance`: `kind` is `agent`, `upload` or `external`. Agent publications include `agent_id`, `agent_name`, `task_id` and `run_id` from the current authenticated execution. Caller-supplied provenance is replaced. Missing provenance on legacy artifacts means the source was not recorded; it is not inferred from filenames or forwarded references. This metadata does not grant file access or add an Agent reading capability.
