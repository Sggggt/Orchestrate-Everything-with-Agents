# Getting started

Orchestrate Everything with Agents runs on your computer. Python 3.12 or newer is required. The browser frontend has no Node.js build step.

Local command work is optional: start with `--allow-trusted-local-execution` to authorize configured programs, then assign an environment and workbench tools. No container engine is required. See [workbench setup](workbench.md) for readiness and the explicit local execution boundary.

## Install from source

Create the Python environment outside the checkout. From the repository root:

```powershell
python -m venv "$env:LOCALAPPDATA\OEA\environment"
& "$env:LOCALAPPDATA\OEA\environment\Scripts\python.exe" -m pip install -c constraints.txt ".[web,mcp]"
& "$env:LOCALAPPDATA\OEA\environment\Scripts\oea.exe" serve --port 8890
```

On Linux or macOS:

```sh
python3 -m venv "$HOME/.local/share/oea/environment"
"$HOME/.local/share/oea/environment/bin/pip" install -c constraints.txt '.[web,mcp]'
"$HOME/.local/share/oea/environment/bin/oea" serve --port 8890
```

Open `http://127.0.0.1:8890`. The loopback page sets an HttpOnly, same-site authentication cookie automatically. SDK and MCP clients read the external `client-token` file; treat that file as an administrator credential.

Windows users can also run `Start-Workspace.cmd`. It creates an external environment, installs the source with pinned constraints, and opens the workspace. `Start-Workspace.ps1 -NoBrowser` runs without opening a browser. The launcher uses `%LOCALAPPDATA%\OEA\workspace`; an existing legacy workspace is discovered and reused. Direct CLI commands default to `%LOCALAPPDATA%\OEA\data` on Windows, or `$XDG_STATE_HOME/oea` (`~/.local/state/oea`) elsewhere. Use `--data-dir` consistently when connecting to a custom directory.

## Your first project

1. Create a project and optionally bind a local folder outside this repository.
2. Add an Agent to that project. Use an LLM Agent for instructions and tools, or a process Agent for your own code.
3. For model-backed Agents, open **Agent 集成 → 配置 → 模型 API**, save the endpoint, model and key, then test the saved configuration.
4. Create a session and start a conversation, or open **工作流画布** to connect reusable steps.

The session overview contains conversation progress, tasks and dependencies, collaboration messages, and shared artifacts as top tabs. New activity is marked with a red dot. Workflows belong to exactly one project; Agents from another project cannot be substituted into them.

## Try without a model

```sh
oea serve --mock --port 8890 --data-dir /absolute/path/outside-the-repository
```

Use a separate empty data directory for mock mode. It registers demonstration Agents in that directory. Normal startup does not populate demonstration sessions, tasks or business records.

After creating a project in that workspace, the deterministic [workflow example](../examples/workflow.py) needs no model:

```sh
python -B examples/workflow.py --data-dir /absolute/path/outside-the-repository --project-id PROJECT_ID
```

## State and backups

SQLite, credentials, profiles, artifacts, logs and backups live outside the source tree on a local disk. Network shares and synchronized storage are rejected for runtime state. Never open a second embedded Runtime against a directory already served by a host; connect with `PlatformClient` instead.

```sh
oea backup --data-dir /absolute/path/to/state --output /absolute/path/to/backup.sqlite
```

Schema upgrades create a backup before migration. Stop the relevant host before replacing its state or restoring a backup. Updating source files does not reload a running Python host; restart it after active work has reached a safe boundary, then reconnect its MCP clients.
