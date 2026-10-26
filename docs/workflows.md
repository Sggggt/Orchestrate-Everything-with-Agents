# Workflows

A workflow is a versioned project-owned graph compiled and executed by LangGraph. The browser canvas edits declarative definitions; registered Python graphs expose their actual topology and are maintained in code.

```python
from oea import WorkflowBuilder

graph = (WorkflowBuilder()
    .node("start", "start")
    .node("format", "template", value={"text": "Hello, {{inputs.name}}."})
    .node("end", "end", outputs={"text": "{{nodes.format.text}}"})
    .connect("start", "format").connect("format", "end").build())
```

Use `workflows.create`, `workflows.validate`, `workflows.run`, and `workflows.result` through the shared registry or host service. Execution operations require the owning `project_id`. A draft may be incomplete; running requires a complete typed graph, valid inputs and current grants. The [executable example](../examples/workflow.py) connects to an existing host.

Agent, map, function, template, condition, loop, subgraph, human wait and join nodes share the persistent runtime. Map uses native `Send` and keeps ordered member receipts. Register deterministic functions in the host; put side effects in authorized tools. Templates read `inputs`, `nodes`, `item`, and `index` without evaluating Python. Parallel joins use `all`; alternative-path merges can use `any`.

Native interrupts and dependency receipts suspend execution. Resume the original workflow, node and interrupt using its original key; do not submit replacement work to recover a response. Results and versioned node evidence remain readable after a failed branch. A root cost budget and cancellation ownership follow actual child work.

See [SDK](sdk.md) and [orchestration interfaces](orchestration/README.md) for current signatures and repair contracts.
