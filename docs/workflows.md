# Workflows

A workflow is a reusable, versioned DAG owned by exactly one project. Its `project_id` cannot change after creation. The canvas, Python SDK, HTTP and MCP share the same graph and run contracts.

## Design on the canvas

Select a project and open **工作流**. Add nodes, edit their properties, and connect output ports to input ports. A condition has `true` and `false` outputs. Drag nodes or enter coordinates; pan the background, zoom, fit the graph or use automatic layout.

Delete removes a selected node or edge; Ctrl+Z and Ctrl+Shift+Z undo and redo; Ctrl+S saves. Drafts may be incomplete. Validation identifies cycles, disconnected nodes, invalid ports, missing Agents and references to non-upstream nodes. Saving uses an expected revision to protect another client's edits.

Export contains the definition and graph, without credentials or run data. Import creates a new definition in the selected project. Referenced Agents must belong to that project. Archive blocks edits and new runs. The canvas's workflow delete button asks for confirmation and moves the definition into a recoverable recycle bin; graph, revision and run history are retained. Active, waiting or uncertain runs prevent deletion. Restore returns the previous active or archived state.

## Node types

| Type | Configuration | Output |
| --- | --- | --- |
| `start` | Object `input_schema`, without schema references; optional `failure_policy` | Run input object |
| `agent` | `agent_id`, `payload`, attempt timeout and retry policy | Complete `TaskResult.output` |
| `map` | `agent_ids`, `items`, per-item `payload`, `max_concurrency`, `routing`, timeout/retry, `result_mode` | Ordered `outputs` and `count`, or explicit settled member records |
| `condition` | `left`, `operator`, `right` | `{"result": true/false}` and selected branch |
| `template` | Text `template` | `{"text": "resolved text"}` |
| `end` | JSON object `outputs` | Final output object |

Conditions support `equals`, `not_equals`, `contains`, `gt`, `gte`, `lt`, `lte`, `is_empty` and `is_not_empty`. Numeric comparisons require numbers. Equality distinguishes strings, numbers and booleans. Fan-out can run independent paths concurrently. A join waits for every incoming path to settle and runs when at least one incoming path is active; unselected branches become `SKIPPED`.

The canvas supports acyclic graphs, not loops, arbitrary script nodes or nested workflows. Put custom code or a framework graph inside a process Agent. Tool calls use the Agent's granted RunContext; a workflow does not expand permissions. Definitions are limited to 100 nodes, 400 edges and 1 MiB of JSON. Runtime limits also apply to inputs and outputs.

## Variables

### Parallel map

A map node resolves an input or upstream list of up to 128 items. Its payload can reference `{{item}}`, `{{item.field}}` and `{{index}}` as well as ordinary upstream variables. Each item creates an independent thread/task in a durable instance batch, using the ordinary scheduler and the selected Agent pool. The default `result_mode="all_succeeded"` requires every member to succeed. Outputs preserve input order even when completion order differs; an empty list succeeds with `outputs=[]` and `count=0`.

Choose `result_mode="all_settled"` to collect each member's `ordinal`, task/thread identity, `state`, complete `result` and `error` in `results`, with `count` and `counts`. Downstream references use `{{nodes.parallel.results}}`. A failed member stays visible; it is not filtered out or replaced with a default value. Waiting and uncertain members do not count as settled. The empty result is `results=[]`, `count=0`, `counts={}`. Collecting a complete failure report does not establish that the original substantive goal succeeded.

```python
graph = (
    WorkflowBuilder()
    .node("start", "start")
    .node("parallel", "map", agent_ids=["worker"],
          items="{{inputs.items}}", payload={"input": "{{item}}"},
          max_concurrency=4, routing="least_loaded")
    .node("end", "end", outputs={"results": "{{nodes.parallel.outputs}}"})
    .connect("start", "parallel").connect("parallel", "end").build()
)
```

The canvas exposes the same map configuration and member states, including links to member conversations. Run records include `batch_id` and member task IDs. Resume or reconcile one member using `instance_index` with `resume_node` / `reconcile_node`; cancellation covers only executions owned by that workflow run and its linked children. First-result and quorum policies are not provided. Existing linear Agent nodes keep their original behavior.

### References

Reference `inputs.name` or `nodes.node_id.field`, with object fields and numeric list indexes. A node may reference only upstream nodes. A whole reference preserves the JSON type; references mixed with text become text.

```json
{
  "input": "Research {{inputs.topic}}",
  "count": "{{inputs.count}}",
  "previous": {"$ref": "nodes.research.text"},
  "optional": {"$ref": "nodes.branch.text", "default": "Branch skipped"}
}
```

Missing references fail unless an explicit literal default is supplied. Templates resolve paths only; they do not execute expressions or code.

## SDK example

```python
from oea import WorkflowBuilder

graph = (
    WorkflowBuilder()
    .node("start", "start", x=60, y=120)
    .node("format", "template", x=370, y=120,
          template="Goal: {{inputs.topic}}")
    .node("end", "end", x=680, y=120,
          outputs={"text": "{{nodes.format.text}}"})
    .connect("start", "format")
    .connect("format", "end")
    .build()
)

async def run(client, project_id):
    workflow = await client.workflows.create(
        project_id, "My workflow", "create-my-workflow-v1", graph=graph)
    receipt = await client.workflows.run(
        workflow["id"], {"topic": "A personal project"}, "my-run-001",
        expected_revision=workflow["revision"])
    return await client.workflows.result(receipt["id"], timeout=10)
```

Use a new run key for a new intended execution; retain the same key to recover an interrupted submission. See the executable [workflow.py](../examples/workflow.py). `WorkflowBuilder.build()` checks graph structure; the service also checks project ownership, Agents and inputs.

Update with `update(workflow_id, expected_revision, graph=..., name=...)`. List returns a small directory; get loads one graph. `node_types()` provides node contracts. `runs()` lists history and `get_run()` returns one run's node states, inputs, outputs, task IDs and errors. `result(timeout=0)` does not wait by default; explicit waits are capped at 30 seconds, and `WAIT_TIMEOUT` does not cancel work.

## Execution and recovery

Starting a run atomically saves its definition revision, graph, inputs and initial states. Supply `session_id` to use an open session in the same project; omission keeps the original behavior of creating a session. Multiple definitions and multiple runs of one definition can share a session. Each executing node has its own thread, bound to exactly one workflow run. `threads.ownership` exposes this origin separately from the original strict thread snapshot. Ordinary turns and execution ownership are tracked separately, so sharing a session or a thread's origin does not implicitly extend a cancellation set.

Agent nodes submit ordinary durable tasks and share runtime budgets, model snapshots and tool authorization. Reusing a run key with different inputs or a different explicit session is a conflict. A receipt retry returns its original revision and session even if the definition has since changed.

The start node defaults to `failure_policy="fail_fast"`. Choose `collect_independent` to let independent branches finish when another branch fails; failed dependencies block their downstream nodes. Successful evidence and partial end outputs remain in `get_run`, while the workflow still fails when required branches failed. An independent waiting node can resume despite another branch's failure. This policy does not release unresolved external work.

Waiting nodes keep their original task. Supply actual input with `resume_node`; message and task waits use normal task wake-up rules. Missing model configuration leaves work queued until the Agent's model settings are saved and tested. Completed nodes are not replayed after host restart.

For a terminal run, `repair(source_run_id, idempotency_key, inputs=None, expected_revision=None, rerun_nodes=())` creates a new run in its open session. It preserves the source graph unless an explicit current definition revision is selected. Failed or changed nodes and their downstream dependants rerun; unchanged successful nodes reference their original results. Map repairs keep successful members with unchanged payloads and rerun only the remaining input ordinals. Explicitly selecting a map in `rerun_nodes` requests all of its members again. `get_repair` records the source run, invalidated nodes, result versions and member provenance. Original terminal runs, tasks and thread ownership remain unchanged; source execution with an unknown outcome must be reconciled first.

An uncertain Agent result stops dependent dispatch. Use `reconcile_node` with actual stop evidence and external results. `cancel(run_id)` requests cancellation of that run's owned tasks and linked child instances; other runs and ordinary threads in the session continue. It remains `CANCELLING` until stops are confirmed, or `UNCERTAIN` when they cannot be established. Completion and waiting checks use the same ownership boundary. Cancellation does not roll back external effects.

The browser highlights active definitions and groups their run instances. The instance selector starts with executing runs, followed by waiting/uncertain and completed runs; creation time and identity break ties. A manual selection survives new runs, definition edits and event refreshes. The canvas displays the selected run's saved graph. `runs(order="active")` exposes this ordering; the default SDK history ordering remains creation order. `activity(project_id)` returns bounded active-run counts. Browser updates use the existing SSE stream.

An unfinished workflow blocks archival, deletion or folder changes of its project and closure/deletion of its run session. A workflow run's session cannot move to another project.

## MCP and skills

Use `platform_guide(area="workflows", action="...")` and `platform_call(area="workflows", action="...", params={...})`. Operations cover definitions, validation, node types, runs, results, cancellation, resume, reconciliation, deletion and restore. The focused [orchestration skill](../integrations/codex/orchestrate-everything-with-agents/workflows/orchestration.md) guides discovery and verification.

Delete uses `delete(workflow_id, confirmed=True)`; `list(project_id, deleted_only=True)` reads the recycle bin and `restore(workflow_id)` restores a definition. Existing `tasks.workflow` remains the separate API for one-off task dependencies inside a session.
