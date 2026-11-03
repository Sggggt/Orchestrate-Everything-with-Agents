# Getting started

OEA uses the `oea` entry point and Python 3.12+. Native local execution is available for Windows x64 and Linux x64 through matching platform wheels. The wheel includes the Rust component; normal installation does not compile Rust or require Node/.NET for that component.

<a id="quick-install"></a>
## Install a platform wheel

Obtain the wheel matching your operating system. In the commands below, replace `PLATFORM_WHEEL.whl` with its full filename. The Windows tag is `win_amd64`; local Linux builds use `linux_x86_64` and the build distribution's C runtime.

```sh
python -m pip install "./PLATFORM_WHEEL.whl[web,mcp,workbench]"
oea --version
oea serve --allow-commands
```

A virtual environment keeps the installation separate from other Python applications. The optional [Windows installer](#windows-shortcuts) manages one for you. For native backend prerequisites and policy limitations, see [native execution](native/README.md). Unsupported platforms or missing capabilities do not silently run commands without isolation.

<a id="start"></a>
## Start the workspace

`oea serve` prints the local browser URL; the initial default is **http://127.0.0.1:8890**. A foreground server stops with Ctrl+C; closing the browser does not stop it. The browser establishes an authenticated local session. Starting again for the same directory reuses the authenticated host instead of opening another host.

```sh
oea paths
oea request host.status
oea mcp
```

`oea paths` reports locations without creating them. Status and MCP need a running host. `--allow-commands` enables configured command/process tools; reconnecting does not change an existing host's grant. Start without that flag when those capabilities are not needed.

Create a project, set its default directories and network policy, then configure an Agent and open a conversation. Model credentials stay in local secret storage. A manager is optional. Main and summary model configurations are separate; an optional manager has its own model/credential configuration. Startup creates no demonstration work or model calls.

<a id="sdk-installation"></a>
## Install into a Python environment

Keep the environment outside the checkout, and use its Python for SDK scripts.

<a id="windows-powershell"></a>
### Windows PowerShell

```powershell
python -m venv "$env:USERPROFILE\.venvs\oea"
& "$env:USERPROFILE\.venvs\oea\Scripts\python.exe" -m pip install "./PLATFORM_WHEEL.whl[web,mcp,workbench]"
& "$env:USERPROFILE\.venvs\oea\Scripts\oea.exe" serve --allow-commands
```

<a id="windows-cmd"></a>
### Windows Command Prompt

```bat
python -m venv "%USERPROFILE%\.venvs\oea"
"%USERPROFILE%\.venvs\oea\Scripts\python.exe" -m pip install "./PLATFORM_WHEEL.whl[web,mcp,workbench]"
"%USERPROFILE%\.venvs\oea\Scripts\oea.exe" serve --allow-commands
```

<a id="linux-macos"></a>
### Linux

```sh
python3 -m venv "$HOME/.venvs/oea"
"$HOME/.venvs/oea/bin/python" -m pip install "./PLATFORM_WHEEL.whl[web,mcp,workbench]"
"$HOME/.venvs/oea/bin/oea" serve --allow-commands
```

Linux also requires the native backend's system dependencies. Installing a CLI environment does not make `import oea` available to another interpreter. Native macOS execution is not part of this release.

<a id="storage"></a>
## Data locations

The default home is `~/.oea`. `data/` contains persistent state, checkpoints, secrets, workspaces and artifacts. The Windows convenience installer uses `runtime/`, `cache/` and `bin/` for its environment, caches and launchers. Other Python installations keep their executable environment wherever it was installed.

State must be on local disk outside source, Git and synchronized folders. `OEA_HOME` explicitly selects another absolute home; `--data-dir` selects a particular state directory. Neither option moves existing data. Retired `OEA_DATA_DIR` and `OEA_PYTHON` variables do not silently select startup locations. If an old location would be abandoned, startup reports relocation requirements instead of creating an empty replacement.

<a id="mcp-client"></a>
## Connect an MCP client

Use command `oea` and arguments `["mcp"]`, with the host already running. The bridge connects to that same state rather than creating another host. Use a stable consumer identity per conversation.

Find the actual executable with `(Get-Command oea).Source` in PowerShell, `where oea` in Command Prompt, or `command -v oea` on Linux. A graphical client may need the absolute executable path. Restart it after PATH or installation changes. See [MCP](mcp.md).

<a id="development"></a>
## Source development

For model/MCP-only source development, install editable code in an external environment:

```sh
python -B -m pip install -c constraints.txt -e ".[web,mcp,workbench]"
```

Keep `PYTHONDONTWRITEBYTECODE=1` and all caches/output outside the checkout. Without native assets, local native tools fail closed. To enable them, build the [native component](native/README.md), set `OEA_NATIVE_ASSETS` to its staged assets directory and reinstall. A standard wheel build requires those matching assets.

Refresh the browser after static frontend changes and restart an idle host after Python changes. See [contributing](../CONTRIBUTING.md).

<a id="windows-shortcuts"></a>
## Windows convenience scripts

```powershell
.\Install-OEA.ps1 -Wheel .\PLATFORM_WHEEL.whl
.\Start-Workspace.ps1
```

The installer creates the managed runtime, installs the wheel and places `oea` and launchers in `~/.oea/bin`. It does not install a compiler. Source builders may instead provide `OEA_NATIVE_ASSETS`; `-Editable` is an explicit development mode. Stop the host before updating its Python environment.

The launcher starts or reuses the host and opens the browser. New hosts enable configured command/process tools by default, including when opening `Start-Workspace.cmd`. Use `-AllowCommands:$false` in PowerShell to start without that grant. It supports `-Port`, `-NoBrowser`, `-AllowCommands`, `-DataDir` and `-Python`. Reusing a host preserves its existing grant; `host.status.allow_commands` and `agents.check` report it. Changing that grant requires restarting the host after its active work is handled. The launcher does not migrate state, install dependencies or modify models.

<a id="update-tool"></a>
## Update existing data and installations

Protocol 3 uses a new state schema and tool/message/budget contracts. Stop the old host, preserve its source snapshot and inspect the [offline conversion](migration/README.md). Apply to a new target, verify it, then point launchers and clients at that target. Do not copy a live SQLite database or overwrite old state. Existing nonterminal work is retained for review and is not replayed under a different protocol.

Install the matching new platform wheel only after the old runtime is idle. Keep source backups and any new facts when considering rollback.

Continue with the [User Guide](user-guide.md), [中文指南](user-guide.zh-CN.md), [SDK](sdk.md) and [workflows](workflows.md).
