<p align="center">
  <img src="src/oea/web/static/mark.svg" width="96" height="96" alt="Orchestrate Everything with Agents logo">
</p>

<h1 align="center">Orchestrate Everything with Agents</h1>

<p align="center">Your agents. Your tools. Your orchestration.</p>
<p align="center">English · <a href="README.zh-CN.md">简体中文</a></p>

Orchestrate Everything with Agents is a lightweight multi-agent development and collaboration platform for individuals. Develop and orchestrate anything within the framework: your own Agents, tools, code, models and workflows, brought together in a workspace on your computer. Start with a conversation, turn repeatable work into a graph, and keep each Agent's context independent while they work toward a shared goal. Of course, if you feel like it, you can let your ChatGPT control it all.

## What you can build

- **Personal Agent workspaces.** Organize Agents, conversations, instructions and skills by project.
- **Your own execution logic.** Use a model Agent, a v2 Python process Agent, or a registered Python graph. LangGraph drives Agent and business execution.
- **Independent parallel instances.** Reuse configurations with separate contexts, stable work directories, ordered results and cancellation along actual ownership.
- **Reusable workflows.** Compose Agent, map, condition, template, function, subgraph, wait and end nodes, including loops and parallel joins. Every workflow belongs to one project.
- **Tools and work artifacts.** Register MCP services, grant selected tools, exchange complete collaboration messages and keep session files with source attribution and downloadable references.
- **Practical work tools.** Grant file edits, images, artifacts and explicitly authorized local commands through one [workbench](docs/workbench.md), with optional external language environments.
- **Durable local execution.** Keep task receipts, checkpoints, run history and backups outside the source tree, with explicit cancellation and uncertain-outcome handling.
- **MCP control.** Connect a compatible client through two compact tools and a focused Codex skill. The browser remains available for configuration and observation.

The frontend is served directly from local assets, with no build step. Saved Agent settings, group messages, durable receipts, complete results and artifacts share the same services as the SDK. Model configuration and credentials are separate for main and manager roles.

## Run locally

Requires **Python 3.12+**. From a source checkout, create an environment outside the repository and install the Web and MCP extras:

```sh
python -m pip install -c constraints.txt '.[web,mcp]'
oea serve
```

Open **http://127.0.0.1:8890**; the loopback page establishes a local authenticated browser session. Use a local state directory outside source repositories and synchronized folders. See [getting started](docs/getting-started.md) for setup.

Create a project, configure an Agent and open a session. Save endpoint, model and credential through Agent settings. Keys stay in local secret storage. `--allow-commands` explicitly enables configured local commands and process Agents with host user permissions. Startup creates no demonstration work.

## Orchestrate with Python

```python
from oea import PlatformClient, WorkflowBuilder

graph = (
    WorkflowBuilder()
    .node("start", "start")
    .node("format", "template", value={"text": "Hello, {{inputs.name}}."})
    .node("end", "end", outputs={"text": "{{nodes.format.text}}"})
    .connect("start", "format")
    .connect("format", "end")
    .build()
)

async def run(data_dir, project_id):
    async with PlatformClient.from_data_dir(data_dir) as client:
        workflow = await client.call("workflows.create", {
            "project_id": project_id, "name": "Hello workflow",
            "graph": graph.model_dump(mode="json")})
        receipt = await client.call("workflows.run", {
            "workflow_id": workflow["id"], "inputs": {"name": "world"},
            "idempotency_key": "hello-run-001", "project_id": project_id})
        return await client.call("workflows.result", {
            "workflow_run_id": receipt["id"], "project_id": project_id,
            "timeout": 10})
```

Use an existing project's ID and the host's external data directory. Keep a key when recovering the same request; choose a new run key for a new intended execution. The executable [workflow example](examples/workflow.py) and [Agent examples](examples/model_agent.py) cover the next steps.

## Let a client operate the workspace

```sh
oea mcp
```

The stdio bridge connects to the running host and exposes `platform_guide` and `platform_call`. Use one stable conversation consumer ID. The supplied [Codex skill](integrations/codex/orchestrate-everything-with-agents/SKILL.md) routes discovery, configuration, execution and receipt reading. See [MCP and skills](docs/mcp.md).

## Documentation

[Getting started](docs/getting-started.md) · [SDK](docs/sdk.md) · [Workflows](docs/workflows.md) · [MCP and skills](docs/mcp.md) · [API](docs/api.md) · [Architecture](docs/architecture.md) · [Module interfaces](docs/modules.md) · [Brand](docs/brand.md)

Runtime state belongs on a local disk outside Git repositories and synchronized folders. Connect to an existing host with `PlatformClient`; one Host owns each state directory. Agents are trusted local programs, and managed subprocesses are not an OS sandbox. Context, action receipts and native graph checkpoints retain their own data ownership during recovery.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks, and [SECURITY.md](SECURITY.md) for sensitive reports. The repository includes generic automated contract tests and source fixtures, with no populated workspace or generated test output.

Licensed under [MIT](LICENSE). Version 2 uses the `oea` package, CLI and protocol. Upgrade existing state through an explicit offline migration into a new directory.
