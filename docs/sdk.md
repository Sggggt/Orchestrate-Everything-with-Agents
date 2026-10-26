# Python SDK

OEA combines an Agent capability kernel with LangGraph. Configure the model, instructions, tools and Skills, context, workspace, execution and communication once, then reuse a persistent instance. The public package is `oea`; version 2 rejects legacy runtime contracts.

Install OEA in the Python environment that runs your application using the [SDK installation steps](getting-started.md#sdk-installation). The recommended `uv tool install` platform setup provides the global `oea` command in an isolated tool environment; it does not make the package importable in another Python environment.

## A standalone Agent

Set `OEA_STATE_DIR` to a local directory outside source repositories and synchronized folders. Configure `OPENAI_BASE_URL`, `OPENAI_MODEL`, and `OPENAI_API_KEY` for an OpenAI-compatible provider. The SDK keeps a credential reference; it does not copy the key into exported Agent configuration.

```python
import os
from pathlib import Path
from oea import Agent, Model, ToolKit, Workspace

with Agent(
    name="reviewer",
    model=Model.from_env(),
    instructions="Read the input, verify it with tools, and publish the result.",
    tools=ToolKit.local(files=True, commands=True),
    workspace=Workspace(project=Path("./project")),
    state_dir=Path(os.environ["OEA_STATE_DIR"]) / "review",
) as agent:
    instance = agent.instance("review-001")
    result = instance.run("Check the source files and deliver a report.")
    print(result.text)
    print(result.artifacts)
```

The project is a read-only input by default. Work and per-run output directories live under the external state directory unless explicitly configured. File tools and commands receive the same real paths. Commands run with the host user's permissions; file grants are not an operating-system sandbox. Selecting command tools in an owned standalone Agent enables its local command host. An existing shared host must also have enabled commands explicitly.

Construction and entering the context do not start a host. First use initializes the owned host, model transport, graph saver, and connections. Closing the Agent closes resources it owns and preserves state and partial files. Reopening the same state directory recovers the saved instance; a second host cannot own that directory concurrently.

## Use a saved Agent

The constructor declares and saves its supplied definition. Use `Agent.from_saved` to execute an existing GUI or SDK definition without saving defaults over it:

```python
from oea import Agent, PlatformClient

async with PlatformClient.from_data_dir(data_dir) as client:
    async with Agent.from_saved("reviewer", client=client, session_id=session_id) as agent:
        result = await agent.arun("Continue the review.")
```

Lookup is lazy. Each new Run uses the service's accepted configuration snapshot; the wrapper does not rewrite the Agent or its secrets. Global definitions require an explicit project or session. An owned, closed SDK state directory can be reopened with `state_dir`; command execution there additionally requires `allow_commands=True`. Connecting to an existing host preserves its own command grant and lifecycle. Default SDK instance identities survive display-name changes.

## Typed function tools

Function tools run synchronous callables in a thread and declare `cancellation="none"` by default. A Python thread can continue after its caller leaves; the original action remains observable until the callable returns. Only coroutine functions can explicitly declare `cancellation="cooperative"`, and must finish their cleanup before propagating cancellation. `owned_process` is reserved for providers that manage and observe an actual process tree.

One declaration supplies parameter validation, model schema, invocation, and JSON result conversion:

```python
from dataclasses import dataclass
from oea import tool, ToolKit

@dataclass
class Total:
    value: int

@tool
def calculate_total(values: list[int]) -> Total:
    return Total(sum(values))

tools = ToolKit.local().with_tools(calculate_total)
```

Pass this toolkit to an Agent. Parameters require annotations and named arguments. Dataclasses, Pydantic models, enums, and collections are normalized before their receipt is saved. Scalar and collection results use a `value` envelope. Declare `side_effects="local"` or `"external"` for actions that change state. The execution boundary owns current authorization, stable keys, receipts, and unknown outcomes.

## Async code and shared hosts

```python
from oea import Agent, Model, ToolKit
from oea.runtime.host import Host

async def review(data_dir):
    async with Host(data_dir) as host:
        async with Agent(
            name="reviewer",
            model=Model.from_env(),
            tools=ToolKit.local(),
            host=host,
        ) as agent:
            return await agent.instance("review-001").arun("Review the input")
```

Several Agents can share one host. Independent instances run directly in parallel; an instance has one context writer. Later explicit work on that instance waits for its current work. Cost budgets are independent of execution admission. Closing an Agent attached to an existing host or client leaves that shared backend open.

Use `arun`, `astream`, and `aclose` inside an async application. Synchronous methods use one owned event loop and reject calls from a running async loop. Keep an Agent and its client on their original event loop.

## Streams, receipts, and continuation

`stream` and `astream` yield progress frames and a final durable result or visible waiting/error state. Progress frames may be lost; read the original run receipt to recover its result. The final `RunResult.text`, structured data, artifacts, and usage remain available independently of streamed tokens.

Supply a stable `idempotency_key` for a logical work submission. A response loss does not authorize another work intent. `instance.last_run_id` identifies the original run, and `host.runs.inspect` reads its errors, actions, partial artifacts, and budget. `host.runs.result` reads or waits for its delivery.

An interrupted or failed graph resumes through `host.runs.resume(run_id, idempotency_key, input=...)`, or `instance.resume` / `instance.aresume`. Resume retains the run, root budget, directory, and action keys. At this explicit boundary it refreshes the model and capability configuration by default; the run service accepts `refresh_model=False` and `refresh_capabilities=False`. Changing the default workspace does not move an existing instance. A completed original model or tool response is read from its receipt. Unknown side effects require inspection before resumption. Ordinary tool parameter errors remain within the existing graph and can be repaired locally.

Cancellation requests and confirmed stops are separate. A function with no cancellation capability can finish after its consumer stops waiting. Its original result is saved; continuation then requires explicit resumption. This does not submit another external action.

Execution completion, deterministic delivery checks, and semantic acceptance are separate. Missing Markdown headings or presentation markers do not trigger retries. A provider timeout or incomplete response retains available evidence and usage estimates.

## Connect to an existing host

```python
from oea import PlatformClient

async def inspect(data_dir):
    async with PlatformClient.from_data_dir(data_dir) as client:
        return await client.call("runs.inspect", {"run_id": "<run-id>"})
```

The client reads loopback connection metadata and its local credential. It verifies protocol `2.0` and never starts a host. It cannot silently connect through an old response view. Pass an open client to `Agent(client=client, project_id=..., session_id=...)` to use an existing collaboration space. Python function implementations must be registered in the host process before binding them remotely.

Model, tools, Skills, workspace, memory, execution, and communication use `AgentDefinition.model_json_schema()`, also available through `agents.schema`. SDK, MCP, and Web consume that shared schema. Secret values are managed separately from configuration.

## Cost budgets and capability use

`host.budgets.create(project_id, Budget(...), idempotency_key, session_id=...)` creates an explicit shared cost ledger. An omitted session allows work in that project to share the budget across sessions. A run can select it with `RunSpec.budget_id`, or `instance.arun(..., budget_id=...)`. Child work inherits its parent's ledger. A workflow and all of its subgraphs, managers, instances, summaries, and repairs keep that same ownership. This budget does not limit execution slots.

Usage separates provider-reported actual tokens, in-flight reservations, and estimates where the provider did not report usage. Cached input remains part of input tokens; billing currency requires provider pricing. A changed instance name does not reset an inherited budget.

Use `UsePolicy(tools=CapabilityUse(allowed=(...), required=(...)), skills=...)` on a run or an Agent/map node. `allowed` narrows saved and current grants. `required` is checked against successful action receipts or persisted Skill loads before delivery. It cannot grant an unbound tool. Empty `allowed` excludes all capabilities in that group; `None` uses the existing bindings.

`agents.check(agent_id, project_id=...)` inspects saved configuration without making model or tool calls. It reports main and manager model credentials independently, current tool grants, executable lookup and a workspace path preview that creates no directories. A missing manager credential cannot fall back to the main credential. These observations establish configuration readiness only; model connectivity, tool execution and task delivery require their own actual receipts.

## Messages and departmental managers

Messages name their actual purpose. `inform` and `report` persist information without starting inference. `request` accepts explicit work under one root budget. `reply` continues the original waiting run or enters its next safe input boundary.

```python
from oea import MessageSpec

receipt = await host.messages.send(MessageSpec(
    session_id=session_id,
    recipients=("instance:" + instance_id,),
    purpose="inform",
    content="The assigned source references are ready.",
    idempotency_key="source-notice-001",
))
```

Receivers may be instance IDs, `department:<agent-id>`, or `all`. The recipient set freezes on acceptance. Text mentions do not create work. A new request can supply an explicit `budget_id`; related work supplies its original root instead. Replies identify `reply_to` or `target_run_id`, and never create another budget. Inbox acceptance, start, input application, completion, and the user's read position are separate facts.

Direct sessions start no manager. Enable collaboration explicitly with `sessions.set_mode(session_id, "collaborative", expected_epoch)`, then address a configured department. `AgentDefinition.manager` owns its model, credentials, instructions, communication, and context settings. Instances inherit the saved main configuration. A manager has restricted instance, assignment, evidence, communication, wait, and cancellation tools; it has no business command tools or administrator client.

A manager turn can end while instances continue. Cancel the root work to request cancellation along actual ownership. `runs.get` then reports the cancellation scope separately from a completed coordination turn. Switching back to direct mode changes subsequent manager authorization and leaves accepted instance work intact.

Explicit `context_refs` share read access to versioned evidence. They do not grant execution of the source tool. `contexts.revoke` withdraws the selected shared read grant; replaying an old send receipt cannot restore it. `read_artifact` reads UTF-8 evidence owned by the instance or currently granted related work, using bounded byte offsets. The controller can read a project-scoped page with `artifacts.read`.

Artifacts retain their project, optional session and actual origin: `run` publications have an owning Run and instance; `upload` and `import` sources do not invent execution owners. Controller uploads first call `artifacts.begin_upload` with a stable key, name, byte count and SHA-256. The authenticated content endpoint streams bytes into managed storage and commits an artifact only when its size and hash match. `artifacts.get_upload` reads the original consumer-owned receipt after a lost response; repeating the same accepted content does not publish another artifact. Uploads are bounded to 50 MiB and do not create Agent instances, model calls or budgets.

`PlatformClient.upload_artifact(path, project_id=..., session_id=..., idempotency_key=...)` hashes and streams an explicitly selected local file without changing it or placing Base64 in JSON. After a crash between the complete file receipt and metadata commit, `artifacts.commit_upload` verifies those original managed bytes before completing the same upload. It does not ask the model to confirm the file or resend content.

Pass `artifact_ids=(...)` in `RunSpec` or `Agent.arun` to assign uploaded or imported evidence to new work. Assignment verifies the project and session before accepting a Run. Instructions expose names and references, while `read_artifact` loads text only when needed. Mentioning an artifact ID in a message or goal grants no access. Managers can forward only evidence they can currently read through `assign_work`; child work retains the original root budget. An instance retains previously accepted conversation evidence, while a new read of the managed original still requires its owning or assigned grant.

A bound `read_image` tool accepts either a workspace `path` or an assigned `artifact_id`. Both routes validate the actual PNG/JPEG/WebP content and retain the existing image and encoded-request limits. Assigned images keep their upload/import origin; reading does not republish them under a fabricated Run owner. Original references and current grants are checked again before image bytes enter a model request.

## Retained model attempts

An explicit retry keeps the original model action identity and records another usage entry. Before replacing a failed response, the host moves its partial response and error into immutable attempt history in the same transaction. It retains one owner for each complete response.

`runs.attempts(action_id, project_id)` lists those attempts. `runs.action(action_id, attempt_id=..., offset=..., limit=...)` reads a bounded page of the selected original response. The attempt ID is its usage ID. Returned pages identify their version so subsequent reads remain stable through recovery. Reading an attempt never calls the model again.

For graph state and action ownership, see [architecture](architecture.md). Persistent graph state contains IDs, stable action keys, and references; full conversation and source text belong to OEA Context.

## Business graphs

`WorkflowBuilder` produces a validated definition, which the host compiles directly into LangGraph:

```python
from oea import WorkflowBuilder

graph = (WorkflowBuilder()
    .node("start", "start")
    .node("review", "map", agent_id="reviewer",
          items="{{inputs.documents}}", input={"document": "{{item}}"})
    .node("end", "end", outputs={"reviews": "{{nodes.review.outputs}}"})
    .connect("start", "review").connect("review", "end").build())

saved = await host.workflows.create(project_id, "Document review", graph)
receipt = await host.workflows.run(saved["id"], {"documents": documents},
    "review-batch-001", project_id=project_id)
result = await host.workflows.result(receipt["id"], project_id=project_id, timeout=60)
```

Map uses native `Send` and ordered per-item receipts. Agent nodes, subgraphs, ordinary typed functions, templates, conditions, loops, human waits, and joins share the same persistent graph runtime. Register a pure function with `host.workflows.register_function(function)` before using a function node. Side effects belong in authorized Agent tools.

The canvas may save a typed, incomplete draft while nodes are being connected. `workflows.validate` reports execution readiness separately. Runs require a complete `WorkflowGraph`, current project grants, available functions, and valid start-schema inputs before accepting work or allocating a budget. Draft storage does not dispatch work.

Templates read `inputs`, `nodes`, `item`, or `index`; they never evaluate Python. `{"$ref": "nodes.previous.value", "default": 0}` supplies an initial loop value. Use `join="any"` at an alternative-path merge or a loop entry. Parallel branches use the default `join="all"`.

Use `workflows.get_run` for native interrupt IDs and `workflows.resume(..., input=..., interrupt_id=..., idempotency_key=...)` for the original wait. Parallel human interrupts can be answered independently. Node results remain available through `workflows.node_result` even when another branch fails. Every workflow operation that reads or changes an execution requires its owning `project_id`. Root workflows own a cost budget inherited by every Agent and subgraph; cancellation follows this ownership.

After fixing a failed child run or a pure function, resume the original graph with `workflows.resume(workflow_run_id, key, project_id=..., node_id=...)`. The node selector limits repair to that failed branch. Omit it to continue the currently failed boundaries. A normal child dependency wait advances from its saved receipts and does not accept a replacement work submission.

Each step records its original native checkpoint. Recovery uses [LangGraph checkpoint replay](https://docs.langchain.com/oss/python/langgraph/use-time-travel), with synchronous durability for those anchors. OEA receipts reuse completed business work, and each repair request is consumed once. Restarting after a known failed repair does not retry it again. Pure projections retain argument and source versions; an accepted side effect cannot be silently rebound to changed inputs.

`node_result` returns the evidence version. Pass its `step_id` and `version` to read the original failed outcome after repair. Set `metadata_only=True` to read the child identity or map count without loading child outputs. `workflows.members` pages through a map's member identities and states; it accepts the same step and evidence version. Successful map collections remain immutable, including an explicitly collected failed member that is repaired later. Map fan-out has no fixed member capacity; result reads retain project, session, and workflow ownership checks.

## Python code graphs

Register trusted Python graph factories with `host.register_graph("review", factory, version="1")`, then use `Execution(graph="review", graph_version="1")`. A factory receives the host's SQLite checkpointer and must compile with that exact saver. It can reuse the nodes in `oea.orchestration.graphs.default` and `AgentState` / `AgentGraphContext` from `oea.orchestration.graphs` to call the same OEA model, context, tool, reply, and delivery capabilities. Runtime clients are injected through graph context.

Saved work retains the graph name and version. Register that version before opening a host that must recover it. `executions.graphs` returns actual topology for read-only display; Python code graphs are maintained in code. Declarative workflows remain editable through their validated definition and revision check.

The default Agent graph derives its loop boundary from the selected root budget. Definition defaults do not impose another per-Agent model-call quota on shared work. `Execution.max_steps` is an optional explicit flow bound; custom code graphs use a 512-step default when it is omitted.

## Business MCP

Tool and delivery schemas must bundle their referenced definitions. Validation resolves local and built-in schema resources without fetching network URLs or files. Tool metadata that reflects known credentials is rejected before it can enter a public catalog or model request; legitimate parameter names such as `api_key` retain their schemas. MCP aliases always require the owning service binding and current project grant, including when an alias is mistakenly placed in a function-name list.

`mcp.call` explicitly tries one discovered business tool using a stable idempotency key and project scope. Existing-host clients supply their stable consumer identity automatically; direct host callers pass `consumer_id`. The returned call identity belongs to the controller's tool intent, without creating an Agent instance, model call or synthetic Run. `mcp.call_result` reads its original action, and `mcp.cancel_call` requests cancellation without claiming that an external effect stopped. Unknown outcomes require evidence through `mcp.confirm_call`; confirmation records facts and never resends the tool invocation. Agent and controller tool calls use the same gateway and action receipts.

Acceptance saves the original arguments even before an action is created. A host restart does not dispatch that intent automatically; an explicit same-key continuation can finish an unstarted action. Once dispatched, unknown external outcomes are inspected rather than replayed. Set `wait=False` to receive the durable call handle and observe its events without waiting for the result.

`mcp.register_directory(directory, project_id)` reads a bounded `mcp.json` containing public `MCPServer` fields. Relative stdio working directories and explicit relative executable paths resolve from that directory. Registration does not connect or invoke tools. `mcp.inspect` observes an existing connection and retained catalog metadata without probing it; `mcp.calls` reads a bounded page of original call receipts. `mcp.disconnect` closes idle owned connections and rejects a disconnect while tool calls are still in flight.

`skills.register_directory(directory, project_id)` reads a standard `SKILL.md` frontmatter name and description and registers that explicit directory. `skills.describe` reads retained metadata and Agent bindings; references remain available through the bounded `skills.files` and `skills.read` operations. Directory registration does not load every reference into model input.

Shared Skills are registered through `skills.register` with a source directory and project scope. `SkillBinding(id=...)` binds a registered Skill; `SkillBinding(id=..., path=..., name=..., description=...)` remains a direct SDK directory binding. Registration validates one `SKILL.md`, stores its content version, and does not scan the whole directory on each model call. Model discovery includes each granted Skill's name and description; optional bodies load only when requested. Shared bindings pin that metadata with the accepted body revision, including when an older revision is selected. Direct bindings use the supplied metadata without reading source files for discovery.

`skills.get`, `read`, `files`, `refresh`, `set_enabled`, and `remove` support inspection and management. Explicit registration can override frontmatter names and descriptions. Refresh reads the source again and adopts any frontmatter name and description while retaining the registration ID. Disabling or removing a registered Skill withdraws its current grant without deleting original files. Loaded references retain their own source evidence.

Save a service with `host.mcp.save(MCPServer(...), expected_revision=...)`, then discover it with `mcp.discover(server_id, project_id)`. A service chooses a `stdio` command or a `streamable_http` URL. Sensitive environment entries and HTTP headers use `environment_refs` and `header_refs` with named `env:` or `vault:` references. Service definitions contain references, never exported secret values.

Bind upstream tool names with `ToolKit.local().with_mcp("documents", tools=("read_document",))` or `MCPBinding`. Discovery returns the exact model-safe aliases and schemas. The first Run discovers an uncached binding on demand. Saved revisions pin schemas; tool changes require a new service revision and explicit capability refresh. Current project, service, Agent, and per-Run grants still apply at invocation and receipt replay.

Business connections are separate from the platform's two control tools. Binding the OEA control server or another server exposing both control tools is rejected. Calls share connections without a capacity pool. MCP cancellation cannot prove an external business action stopped: unresolved calls retain their original receipt, and a late result settles that receipt without starting replacement work.

Loopback model and MCP endpoints connect directly rather than inheriting a system or environment proxy. Remote endpoints continue to use the host's configured proxy environment.

Changing a service revision retires old idle connections. An in-flight call retains its original connection until its actual outcome is saved; settlement events then close the retired connection. There is no polling or connection-capacity admission queue.

MCP services default to `session_scope="consumer"`: each Agent instance and controller conversation has its own upstream session. Explicit `session_scope="shared"` reuses a service session across callers and projects within the saved service grant. This controls upstream conversational state, without limiting concurrent calls. `allowed_tools=None` grants the discovered service catalog; a tuple narrows it, and an empty tuple grants none. Agent bindings and per-Run policy can only narrow that service grant. `expose_to_codex=False` prevents new controller tool calls while still allowing authorized Agent bindings. Reading an existing caller-owned receipt remains possible after withdrawal.

If a disconnected original invocation cannot return, the controller first verifies its actual external effect. `runs.confirm_action(action_id, project_id, idempotency_key, evidence, outcome, result=...)` records that verification on the original receipt. A completed confirmation must match its saved tool result contract; `not_applied` records the verified absence of the effect. Live owned calls cannot be manually confirmed. Confirmation is project-scoped and idempotent, and preserves the old error and concrete verification evidence. It never dispatches or resumes work. Explicit continuation remains a separate `runs.resume` operation.

## Compact platform MCP

Connect `oea mcp` to an already running host. It owns a `PlatformClient`, and never opens a second host on that directory. The only tools are `platform_guide` and `platform_call`, backed by the same operation registry as SDK and HTTP.

Keep one stable `consumer_id` per conversation. Discover one area/action, then call that operation. Long results return `result_ref`; use `results.read` with its original project, consumer, and an optional JSON Pointer. Pages read the domain owner's data and never repeat a mutation. Configuration references pin revisions and report an explicit version change instead of returning a replacement configuration.

## Process Agents

When instances intentionally share a writable file, the host serializes file-tool revision checks and replacements for that canonical path. A stale `expected_sha256` returns `REVISION_CONFLICT`; different files remain independent. This protection applies to cooperating file tools in one host. Shell programs and external editors use operating-system file access and can overwrite those files independently. Shared read/modify/write commands need an explicit application lock or a designated writer; SQLite transactions and atomic rename alone do not make arbitrary shell edits transactional.

`Execution(program=(...))` runs an explicitly configured program under the host's command grant. The v2 SDK in `oea.stdio` exchanges UTF-8 JSONL on stdout and writes diagnostics to stderr. A process receives only its Run identity, input, workspace paths, capability descriptions, and explicit checkpoint. It calls the same authorized tools and model through its Run context; it receives no administrator client or model credentials.

Programs declare their implemented cancellation and suspension capabilities. They return a completed or suspended boundary, with a small JSON checkpoint when needed. The host persists a terminal receipt before acknowledging it. A resumed suspension starts a new process from that checkpoint and the original reply; it cannot depend on previous process memory. Managed process termination confirms local process cleanup, while any unresolved external tool action remains pending review. Processes retain host user permissions.

Reader cursors are scoped to the client's stable `consumer_id`. `sessions.unread` aggregates group-message and instance-reply counts; `instances.mark_read` advances only the selected instance. Use `PlatformClient(..., agent_name="example-agent")` to attribute new external messages. This display name grants no capability or authority.
