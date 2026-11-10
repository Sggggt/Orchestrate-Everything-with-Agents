# Programmable Agent architectures

An Agent architecture controls one Agent's model calls, tool use, state, branches and delivery. A [business workflow](workflows.md) coordinates separate Agents and program steps. Both compile to LangGraph and use the same OEA authorization, budgets, durable receipts and checkpoints.

The default Agent keeps its existing ReAct driver. A published architecture is an explicit alternative; publishing a candidate does not change any Agent's binding.

## Canvas workbench

The Agent settings page separates the active immutable version from the candidate being viewed. Select a candidate to change the preview; click the preview itself to enter the full-screen workbench. The editor supports all 13 Agent primitives, typed state and reducers, input/output mappings, conditional paths, cycles, pure parallel regions, nested subgraphs, and capability narrowing. Python functions come from the host's registered pure-function directory; arbitrary Python graphs remain read-only and are registered through the SDK.

Drag nodes and ports to edit, use Shift for selection, Space to pan, and Ctrl+S to save. Undo/redo and copy/paste operate on declarations. Viewport movement does not mark a draft as changed. The editor preserves advanced fields and reports concurrent revisions without overwriting local edits. Trial runs use a dedicated session and independent instance; they incur real model/tool costs and do not change the active binding. Closing the panel does not cancel a Run.

New browser candidates set `owner_agent_id`. `list(owner_agent_id=...)` reads that Agent's candidates; copying another architecture creates a new identity. `save(..., name=...)` can rename a candidate. `remove(architecture_id, project_id, owner_agent_id, expected_revision)` hides and freezes the candidate while retaining all immutable versions, bindings and execution evidence. Deleted candidate identities cannot be reused. Legacy project-shared drafts remain available for explicit copying.

Saving does not activate a graph. The settings page's **切换版本** confirmation publishes the exact saved revision and then binds it using the Agent's expected configuration revision. A binding failure leaves the previous active version intact. A published candidate may therefore exist without being active; reconcile the saved binding after a connection failure. Deleting a candidate also requires confirmation. Published versions remain available for explicit rollback.

## Create, validate and try a candidate

Connect to an existing host with `PlatformClient`. `ArchitectureClient` also accepts an open `Host` owned by your application. It does not own either object's lifecycle.

```python
from oea import (
    AgentArchitecture, ArchitectureBinding, ArchitectureBuilder,
    ArchitectureClient, PlatformClient, RunSpec,
)

async def try_architecture(project_id, instance_id):
    async with PlatformClient.from_data_dir() as client:
        architectures = ArchitectureClient(client)
        template = await architectures.templates("react")
        graph = ArchitectureBuilder(template["graph"]).build(max_steps=256)
        saved = await architectures.create(
            AgentArchitecture(id="review-loop", project_id=project_id, name="Review loop"),
            graph,
        )
        report = await architectures.validate("review-loop", project_id)
        if not report["valid"]:
            return report  # errors identify node and field paths
        published = await architectures.publish("review-loop", project_id, saved["revision"])
        binding = ArchitectureBinding(architecture_id="review-loop", version=published["version"])
        run = await architectures.run(project_id, binding, RunSpec(
            instance_id=instance_id,
            input="Complete the requested review using the available evidence.",
            idempotency_key="review-trial-1",
        ))
        return await client.call("runs.result", {"run_id": run["id"], "timeout": 300})
```

The [executable example](../examples/agent_architecture.py) accepts explicit project, Agent and instance IDs. Trials use that instance's existing permissions and model, without rebinding its Agent. Choose a separate instance when you need independent context. A trial's effects are real; use the same authorization and receipt checks as ordinary work.

`save` replaces the complete draft at an `expected_revision` and permits incomplete declarations. `adjust` changes only exposed options: `max_steps`, and the unchanged Reflexion template's `max_attempts`. Advanced fields remain intact. `publish` validates and creates an immutable version; retrying the same revision returns the original version. `compare` lists changed paths, and `export` returns reproducible Python source. Exporting or editing a file does not register code or switch execution.

Use `bind(agent_id, project_id, binding, expected_revision, role="main")` to apply a version. The optional manager inherits main execution unless explicitly overridden; `role="manager"` changes only that override. Binding and `agents.save` change the default for subsequently accepted Runs, including new work in an existing instance. Accepted, running, waiting and review work retains its original architecture snapshot. Unsettled usage and historical outcomes do not block a default change. `status` reports `activation_scope="future_runs"` and no work-state blocker. Project ownership, permissions, graph validation and expected revisions remain authoritative. Other Agents and independent instances have no resource admission limit.

## State and primitives

`ArchitectureGraph` is a round-trippable declaration with `nodes`, `edges`, `entry`, typed `state`, `max_steps`, `source` and `metadata`. `ArchitectureBuilder` exposes `state`, `node`, `connect` and `build`. Values such as `$counter` read a state field; nested lists and dictionaries support the same references. Node `inputs` map read values into local inputs. `outputs` map named primitive results to declared custom fields. Missing or incorrectly typed mapped results fail at that boundary.

A model node's optional `instruction_field` reads a string from state or mapped local inputs and appends it to its literal instruction. This lets deterministic computation supply model evidence without an additional model call. Literal instructions are never treated as executable expressions.

| Primitive | Purpose |
| --- | --- |
| `prepare` | Assemble work input and check current capabilities |
| `model` | Call the Run's configured model; optionally narrow tools and add an instruction |
| `tools`, `observe` | Execute the original model calls through receipts and choose continue/wait |
| `recover_context` | Apply the existing bounded recovery after a confirmed window rejection |
| `wait` | Persist a LangGraph interrupt; resume with the original Run and reply |
| `deliver` | Retain the full response and check explicit delivery requirements |
| `set`, `condition` | Deterministic state updates and true/false branches |
| `function` | Invoke a registered pure Python function |
| `remember` | Persist model text as a context source and return its reference |
| `dispatch` | Use native `Send` for isolated pure members |
| `subgraph` | Execute a nested declaration, with explicit custom input/output mappings |

Discover one primitive's configuration with `architectures.node_types(kind=...)`. The default node policy inherits Run permissions; an explicit `policy` only narrows tools or Skills. The tool executor also checks the originating model node's exposed tool contracts and current revocations. A role or template never grants extra authority.

State supports strings, integers, numbers, booleans, objects, arrays and `any`. Reducers are `replace`, `append`, `sum` and `merge`, with type-compatible defaults. Parallel regions contain pure set/function nodes and meet at an explicit `join="all"` successor; writes to the same field require a reducer. Model, tool and persistent Context updates remain sequential within an instance. Dynamic members target a pure set/function node and merge reducer outputs; an empty collection still reaches the merge. Nested graphs return mapped state; only the outer delivery publishes the Run result. Use native code graphs for more complex parallel capability ownership.

Each parallel branch receives the fork's state in an isolated native subgraph and publishes its reducer updates at the merge. Intermediate writes are not visible to sibling branches.

Conditions provide both labelled branches. Model decisions provide `tools`, `deliver`, `wait` and `recover_context` branches, or a deliberate single default successor. Templates expose explicit recovery and waiting paths for every model stage. Every node must be reachable and have a delivery path. Static validation does not execute models or tools, or prove that an exit condition will eventually become true. `max_steps` and the original budget bound cycles.

Keep state small. Node updates are limited to 32 KiB; long model responses, observations, plans and reflections belong in persistent services and are represented by IDs or references in checkpoints.

## Templates and evidence

| Template | Behavior |
| --- | --- |
| `react` | Decide, execute tools, observe, wait or deliver; no extra planning model call |
| `plan_execute` | Save an ordered plan and step results; continue, replan on `REPLAN`, or deliver on `PLAN_DONE` |
| `reflexion` | Save the attempt, evaluate it, reflect on feedback and retry within the attempt bound; `ACCEPT_RESULT` selects the saved attempt for delivery |

These are editable declarations, not hidden execution engines. Instantiating a template produces an independent draft. The text markers are template routing conventions; edit the corresponding conditions and instructions together for another acceptance policy. Model evaluation is evidence to inspect, not a guarantee of task correctness.

`inspect(run_id, project_id, node_id=..., cursor=..., limit=...)` returns actual node activations. `node_result` reads one activation's small output and error. Full model/tool bodies remain available through the original Run action and context references. `preview` defaults to at most eight stages and a bounded set of paths. `preview(detail="canvas")` returns a cached topology of at most 128 nodes and 512 edges, including node positions and output ports, without node configuration. `status(canvas=True)` provides the known topology for an unbound registered graph. `functions()` paginates the pure-function directory; `functions(name=...)` reads one function contract without executing it. Both drafts and published versions read a saved summary without fetching or traversing the full declaration. Optional `metadata.preview_stages` selects known stage IDs for display and never changes execution.

Runs pin the accepted configuration and published version. Node action identities include Run, graph scope, node and activation, so loops, nested graphs and dynamic members are distinct. Re-entry after a committed model/tool receipt reuses that receipt. Confirmed context recovery keeps the original logical model action and archives the earlier attempt. Timeouts and unknown side effects stay pending for inspection; a reflection loop cannot authorize blind replay.

Published LangGraph dependency versions are checked before compilation. A mismatch stops execution before any model/tool action; restore those versions to resume the original Run. Publish a new candidate for new work after an intentional dependency upgrade. Pure function and native factory implementations remain the registering application's versioned source responsibility.

## Native Python graphs

Register trusted code explicitly in the application that owns the host:

```python
host.register_graph(
    "review", build_graph, version="2",
    required_tools=("read_context",),
    source={"path": "agent_graph.py", "symbol": "build_graph"},
)
```

The factory receives the host's persistent checkpointer and must compile with that exact saver. Register each name/version once; register required pure functions with `host.workflows.register_function`. Required tools/Skills describe availability and do not grant permission. Native code must use `RunContext` for capability actions and preserve its receipt identities and current authorization checks.

Create an architecture with `representation="python"` and a draft referencing `graph` and `graph_version`. Its preview shows known compiled topology and marks omitted dynamic paths. Editing Python source and registering a new version are separate explicit steps; MCP never executes an uploaded Python string, and the browser never rewrites code from a diagram.

Earlier state schemas require [offline transition](migration/README.md) into a new directory. Unverified old checkpoints are archived and their unfinished work remains for review; normal startup never changes the source database or resumes it using new node names.
