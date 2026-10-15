# Python SDK

The public package is `oea`. The existing `ma_runtime` and `agent` imports and `multi-agent` command remain compatible. New code should use `oea`, `oea.stdio` and the `oea` command.

## Connect to an existing host

```python
import asyncio
from oea import PlatformClient

async def main():
    async with PlatformClient.from_data_dir() as client:
        projects = await client.projects.list()
        print(projects)

asyncio.run(main())
```

Pass the same external `data_dir` used by the host if it is not the CLI default. The client reads loopback connection metadata and a local token; no token belongs in application source. Remote client methods return JSON dictionaries. `client.call(method, params)` exposes the RPC catalog.

Common services are `projects`, `sessions`, `threads`, `agents`, `tasks`, `messages`, `artifacts`, `workflows` and `mcp`. A project groups Agents and sessions; a session groups independent Agent threads. Tasks are durable units of execution. Messages exchange complete context, and artifacts share files by reference.

## Develop a process Agent

```python
from oea import AdapterEvent, AgentDescriptor, TaskResult
from oea.stdio import run_agent

class Echo:
    def describe(self):
        return AgentDescriptor(
            agent_id="echo", execution_mode="subprocess",
            cancellation="terminate_owned_process", supports_probe=False,
            message_receipts="unsupported",
        )

    async def execute(self, request, context):
        text = str(request.payload.get("input", ""))
        yield AdapterEvent(
            type="completed",
            result=TaskResult(output={"text": text}, summary_markdown=text),
        )

    async def aclose(self):
        pass

if __name__ == "__main__":
    run_agent(Echo())
```

Register the program as `kind=process` with an explicit command array such as `[PYTHON, "-B", ABSOLUTE_AGENT_PATH]`. The descriptor's `agent_id` must match the registration. Advertise cancellation, probing, checkpointing and forks only when implemented. See [integration_agent.py](../examples/integration_agent.py) for the full adapter contract and tool use.

`request.project_id`, `request.project_folder`, `request.agent_md`, `request.skills` and the run's configuration snapshot provide context. Read skill bodies only when needed. `context.list_tools()` and `context.invoke_tool()` expose tools authorized for that Agent. `context.send_message()`, `submit_task()` and `put_bytes()` exchange work and artifacts within checked scopes. Do not create an administrator client inside an Agent to bypass run permissions.

For a model-backed process Agent, set `llm_required=True` and call `await context.llm.complete(...)`. The runtime supplies the model and credentials configured in the frontend. `context.llm.info()` returns non-secret settings; model tool calls must still be executed through authorized context methods. See [model_agent.py](../examples/model_agent.py). A mock test proves transport behavior; real model acceptance requires saved credentials and a successful model test.

STDIO carries UTF-8 JSONL only; write logs to stderr. Complete an execution with a `completed` or `suspended` boundary. Process memory is not durable across executions; use checkpoints and artifacts. The subprocess boundary provides lifecycle management, not an operating-system sandbox.

## Output and collaboration

`TaskResult.output` preserves the full collaboration result. `summary_markdown` is the concise browser view. Plain Markdown is accepted. Optionally separate a visible introduction from longer details with `<!-- agent:details -->`; missing markers do not trigger a model retry.

Use stable idempotency keys for submissions and tool invocations. If an action becomes `UNCERTAIN`, inspect its receipt and actual external effect before reconciling it. Do not repeat the action with a new key to obtain a missing response.

## Embed the runtime

```python
from oea import Runtime

async def inspect(data_dir):
    async with Runtime.open(data_dir) as runtime:
        return await runtime.projects.list()
```

Embedded services return public Pydantic types. Only one host owns a state directory. The runtime schedules work, persists receipts and enforces budgets for all adapters. A framework such as LangGraph can run inside an Agent; its internal graph does not automatically become a platform canvas. See [workflows](workflows.md) for the shared graph API.
