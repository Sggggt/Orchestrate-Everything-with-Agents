<p align="center">
  <img src="src/ma_runtime/web/static/mark.svg" width="96" height="96" alt="Orchestrate Everything with Agents logo">
</p>

<h1 align="center">Orchestrate Everything with Agents</h1>

<p align="center">Your agents. Your tools. Your orchestration.</p>
<p align="center">English · <a href="README.zh-CN.md">简体中文</a></p>

Orchestrate Everything with Agents is a lightweight multi-agent development and collaboration platform for individuals. Develop and orchestrate anything within the framework: your own Agents, tools, code, models and workflows, brought together in a workspace on your computer. Start with a conversation, turn repeatable work into a graph, and keep each Agent's context independent while they work toward a shared goal. Of course, if you feel like it, you can let your ChatGPT control it all.

## What you can build

- **Personal Agent workspaces.** Organize Agents, conversations, instructions and skills by project.
- **Your own execution logic.** Use an LLM Agent, write a Python process Agent, or wrap a framework such as LangGraph.
- **Independent parallel instances.** Reuse Agent configurations with separate contexts, Agent-pool routing, batch limits, ordered results and cancellation.
- **Reusable workflows.** Connect start, Agent, parallel map, condition, template and end nodes on a visual canvas or with the same Python SDK. Every workflow belongs to one project.
- **Shared tools and artifacts.** Register MCP services, grant selected tools, exchange complete collaboration messages and share files by reference.
- **Practical work tools.** Grant file edits, images, artifacts and explicitly authorized local commands through one [workbench](docs/workbench.md), with optional external language environments.
- **Durable local execution.** Keep task receipts, checkpoints, run history and backups outside the source tree, with explicit cancellation and uncertain-outcome handling.
- **MCP control.** Connect a compatible client through two compact tools and a focused Codex skill. The browser remains available for configuration and observation.

The frontend is served directly from local assets, with no build step. Task dependencies, collaboration messages and shared artifacts live in the session overview's top tabs; red dots flag new activity. The workflow canvas includes validation, revisions, import/export, deletion and a recycle bin.

## Run locally

Requires **Python 3.12+**. From a source checkout, create an environment outside the repository and install the Web and MCP extras:

```sh
python -m pip install -c constraints.txt '.[web,mcp]'
oea serve --port 8890
```

Open **http://127.0.0.1:8890** ; the loopback page establishes a local authenticated browser session automatically. Windows users can run **Start-Workspace.cmd** for environment setup and startup. See [getting started](docs/getting-started.md) for platform-specific environment commands and data paths.

Create a project, add an Agent and open a session. For model-backed Agents, save the endpoint, model and key in **Agent 集成 → 配置 → 模型 API**, then test the saved configuration. Keys are stored locally, outside this repository.

For a model-free trial, run `oea serve --mock --port 8890 --data-dir EXTERNAL_DIRECTORY` with a separate empty directory. Normal startup does not seed demonstration sessions or tasks.

## Orchestrate with Python

```python
from oea import PlatformClient, WorkflowBuilder

graph = (
    WorkflowBuilder()
    .node("start", "start")
    .node("format", "template", template="Hello, {{inputs.name}}.")
    .node("end", "end", outputs={"text": "{{nodes.format.text}}"})
    .connect("start", "format")
    .connect("format", "end")
    .build()
)

async def run(data_dir, project_id):
    async with PlatformClient.from_data_dir(data_dir) as client:
        workflow = await client.workflows.create(
            project_id, "Hello workflow", "hello-definition-v1", graph=graph)
        receipt = await client.workflows.run(
            workflow["id"], {"name": "world"}, "hello-run-001",
            expected_revision=workflow["revision"])
        return await client.workflows.result(receipt["id"], timeout=10)
```

Use an existing project's ID and the host's external data directory. Keep a key when recovering the same request; choose a new run key for a new intended execution. The executable [workflow example](examples/workflow.py) and [Agent examples](examples/model_agent.py) cover the next steps.

## Let a client operate the workspace

```sh
oea mcp --connect --compact --data-dir EXTERNAL_DIRECTORY
```

The bridge connects to the running host and exposes `platform_guide` and `platform_call`. Copy the generated configuration from the frontend, then install the supplied [Codex skill](integrations/codex/orchestrate-everything-with-agents/SKILL.md). See [MCP and skills](docs/mcp.md) for setup, discovery and access boundaries. A ChatGPT connection requires a client environment that can reach the chosen MCP transport.

## Documentation

[Getting started](docs/getting-started.md) · [SDK](docs/sdk.md) · [Workflows](docs/workflows.md) · [MCP and skills](docs/mcp.md) · [API](docs/api.md) · [Architecture](docs/architecture.md) · [Brand](docs/brand.md)

Runtime state belongs on a local disk outside Git repositories and synchronized folders. Connect to an existing host with `PlatformClient`; do not open a second Runtime on the same directory. Agents are trusted local programs, and managed subprocesses are not an OS sandbox. The workflow graph is a DAG; loops and custom code belong inside an Agent.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks, and [SECURITY.md](SECURITY.md) for sensitive reports. The repository includes generic automated contract tests and source fixtures, with no populated workspace or generated test output.

Licensed under [MIT](LICENSE). Existing `ma_runtime` imports and the `multi-agent` command remain compatible; new integrations use `oea`.
