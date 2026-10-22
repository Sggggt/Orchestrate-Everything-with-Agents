# Python SDK

File, image, command and delivery capabilities use the shared [workbench and environments](workbench.md). `client.environments` prepares existing external programs; `client.workbench` inspects grants and receipts.

Shared Skill registration, Agent associations and task policies are described in [Skills and execution policies](skills.md).

Execution adapters receive a run-bound context. `inbox()` reads pending messages, `acknowledge_messages()` records contiguous transport receipt, and a committed boundary declares application. When the host advertises logical reconnection, `reconnect_channel(connection_id)` changes its generation within the same execution; replay the same ID after response loss. It does not recreate an execution process or an operating-system transport. See [message and channel contracts](api.md).

Adapters that retain a boundary for probing may implement the optional synchronous `discard_committed_boundary(run_id)` hook. Runtime calls it only after the durable commit is confirmed, including commit replay and recovery. Built-in adapters then release their temporary copy; full results remain in the store. Unconfirmed boundaries remain available for probing, and this hook does not release execution resources or stop a worker.

For active diagnostics, passive receiving and run-bound Agent reports, see [Operations and guidance](operations.md). `client.operations` uses the same service as MCP and HTTP RPC; process Agents use `context.report_issue` and the scoped coordinator router. The browser reflects execution and pending reports through Agent status colors.

The public package is `oea`. Use `oea.client` for an existing host, `oea.execution` for adapter contracts, `oea.context` for run-bound capabilities, `oea.tools` for tool contracts and `oea.workflows` for graph definitions. The existing top-level `oea`, `ma_runtime` and `agent` imports and `multi-agent` command remain compatible. Process entry points use `oea.stdio`.

## Connect to an existing host

```python
import asyncio
from oea.client import PlatformClient

async def main():
    async with PlatformClient.from_data_dir() as client:
        projects = await client.projects.list()
        print(projects)

asyncio.run(main())
```

Pass the same external `data_dir` used by the host if it is not the CLI default. The client reads loopback connection metadata and a local token; no token belongs in application source. Remote client methods return JSON dictionaries. `client.call(method, params)` exposes the RPC catalog.

Common services are `projects`, `sessions`, `threads`, `agents`, `tasks`, `messages`, `artifacts`, `workflows` and `mcp`. A project groups Agents and sessions; a session groups independent Agent threads. Tasks are durable units of execution. Messages exchange complete context. Artifacts store session-owned work files and provenance; their references can be included in messages and results. A reference alone does not give an Agent a text-reading tool.

`client.budgets` creates and inspects cumulative execution limits. Bind a budget with the optional `budget_id` submission parameter when independent tasks or workflows should share a cap; descendant tasks and repairs inherit it automatically. The SDK selects response view `1.1`; use `view_version="1.0"` only for legacy response models. See [budget and view contracts](api.md#cumulative-execution-budgets).

## Run independent instances

Reuse an Agent configuration while creating independent threads and tasks:

```python
batch = await client.instances.spawn({
    "session_id": session_id,
    "agent_ids": ["worker"],
    "payloads": [{"input": "First item"}, {"input": "Second item"}],
    "idempotency_key": "two-items-001",
    "max_concurrency": 2,
    "routing": "round_robin",
})
result = await client.instances.result(batch["id"], timeout=30)
```

Each member returns `thread_id`, `task_id`, `agent_id` and its input ordinal. Spawn accepts 1–128 payload objects and creates the whole batch atomically. Reusing the same key and arguments returns the original batch; changed arguments conflict. `get(include_outputs=True)` reads individual results, `list(session_id)` pages summaries, and `cancel(batch_id)` requests cancellation. Results preserve input order. A batch summarizes its original tasks; subsequent `threads.turn` calls continue an instance but do not change that batch's membership.

An Agent pool can contain multiple same-project managed Agents. `round_robin` distributes inputs in order; `least_loaded` prefers enabled, configured Agents, then compares queued/running/uncertain work against declared capacity. Routing assignments are durable and do not change after creation. Global, per-Agent, adapter and batch limits all apply. New runtime defaults are 16 total slots with one reserved; built-in Agent configurations default to four concurrent runs. Existing saved configurations are preserved. Custom adapters remain limited by their actual descriptor.

Inside a process Agent, use `await context.spawn_instances(spec)` with the current session. It requires collaboration permission and records the parent task. Suspend with a `WaitCondition(kind="tasks", task_ids=...)` to release the parent slot; child results arrive in `request.resume_input["tasks"]`. Parent failure or cancellation requests cancellation of unfinished child batches. Do not synchronously wait inside an active parent or create an unmanaged worker pool.

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

When supported by the provider, save `reasoning_effort` in the Agent configuration to control reasoning cost. The default is unspecified, so existing requests remain unchanged. This is a host setting, not a per-call override available to execution code. Output limits include provider reasoning tokens; a response that consumes its limit without visible delivery remains an incomplete result.

STDIO carries UTF-8 JSONL only; write logs to stderr. Complete an execution with a `completed` or `suspended` boundary. Process memory is not durable across executions; use checkpoints and artifacts. The subprocess boundary provides lifecycle management, not an operating-system sandbox.

Model requests permit up to 8 MiB of encoded JSON and model responses up to 2 MiB; managed Agent frames permit up to 16 MiB. These byte limits allow large configured contexts across the process boundary without changing task, message or artifact-upload limits. The saved context budget, output reservation, cumulative usage limits and provider context window still apply independently. A configured 500K-token context is not proof that a particular gateway accepts that input size.

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
