# Getting started

OEA uses the `oea` entry point and Python 3.12+. Native local execution is available for Windows x64 and Linux x64 through matching platform wheels. The wheel includes the Rust component; normal installation does not compile Rust or require Node/.NET for that component.

<a id="quick-install"></a>
## Install a platform wheel

Download the wheel matching your operating system from [OEA 3.4.2](https://github.com/Sggggt/Orchestrate-Everything-with-Agents/releases/tag/v3.4.2):

| System | Download |
| --- | --- |
| Windows x64 | [Windows wheel](https://github.com/Sggggt/Orchestrate-Everything-with-Agents/releases/download/v3.4.2/orchestrate_everything_with_agents-3.4.2-py3-none-win_amd64.whl) |
| Linux x64 | [Linux wheel](https://github.com/Sggggt/Orchestrate-Everything-with-Agents/releases/download/v3.4.2/orchestrate_everything_with_agents-3.4.2-py3-none-linux_x86_64.whl) |
| Integrity check | [SHA256SUMS](https://github.com/Sggggt/Orchestrate-Everything-with-Agents/releases/download/v3.4.2/SHA256SUMS) |

Run the installation command from the directory containing the downloaded file. Windows uses `win_amd64`; Linux uses `linux_x86_64`. The Linux wheel is built on Ubuntu 22.04, requires a compatible C runtime, and is not a manylinux wheel. Native Linux tools also require host policy to permit unprivileged user and network namespaces.

Windows users should complete the [Visual C++ runtime prerequisite](#windows-runtime) before starting native tools.

```sh
python -m pip install "./orchestrate_everything_with_agents-3.4.2-py3-none-win_amd64.whl[web,mcp,workbench]"
oea --version
oea serve --allow-commands
```

For Linux, use the `linux_x86_64` filename instead. A virtual environment keeps the installation separate from other Python applications. The optional [Windows installer](#windows-shortcuts) manages one for you. For native backend prerequisites and policy limitations, see [native execution](native/README.md). Unsupported platforms or missing capabilities do not silently run commands without isolation.

<a id="windows-runtime"></a>
### Windows x64 runtime prerequisite

MSVC-built Windows wheels require the **Microsoft Visual C++ v14 Redistributable (x64)**, including `VCRUNTIME140.dll`. Install or repair the [current official x64 package](https://aka.ms/vc14/vc_redist.x64.exe), then follow any restart request from its installer. Microsoft requires a runtime version at least as recent as the MSVC toolset used to build the application; see its [supported runtime guidance](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist). The redistributable provides runtime libraries; Visual Studio and C++ build tools are not required for a prebuilt OEA wheel.

You can inspect the registered x64 runtime from PowerShell:

```powershell
Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' |
    Select-Object Installed, Version
```

`Installed = 1` indicates a registered runtime. After starting OEA, run `oea request host.status` and check `native_component.state` and the backend capability report to verify that the component actually loads. A `VCRUNTIME140.dll` error or native-component startup failure calls for checking this prerequisite. The required Windows 11/PSEC capabilities still apply.

`Install-OEA.ps1` installs OEA's Python environment and wheel; it does not install this system runtime. The OEA wheel does not bundle the Visual C++ runtime DLLs.

<a id="start"></a>
## Start the workspace

`oea serve` starts the local Host; the initial origin is **http://127.0.0.1:8890**. Run `oea open` in another terminal to open an authenticated browser session. For a custom state directory, pass the same `--data-dir` to both commands. `oea open --print-url` prints a single-use login link when automatic browser opening is unavailable. The link expires after 60 seconds and does not contain the long-lived control credential. A plain homepage visit never grants management access. A foreground server stops with Ctrl+C; closing the browser does not stop it. Starting again for the same directory reuses the authenticated host instead of opening another host.

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
& "$env:USERPROFILE\.venvs\oea\Scripts\python.exe" -m pip install "./orchestrate_everything_with_agents-3.4.2-py3-none-win_amd64.whl[web,mcp,workbench]"
& "$env:USERPROFILE\.venvs\oea\Scripts\oea.exe" serve --allow-commands
```

<a id="windows-cmd"></a>
### Windows Command Prompt

```bat
python -m venv "%USERPROFILE%\.venvs\oea"
"%USERPROFILE%\.venvs\oea\Scripts\python.exe" -m pip install "./orchestrate_everything_with_agents-3.4.2-py3-none-win_amd64.whl[web,mcp,workbench]"
"%USERPROFILE%\.venvs\oea\Scripts\oea.exe" serve --allow-commands
```

<a id="linux-macos"></a>
### Linux

```sh
python3 -m venv "$HOME/.venvs/oea"
"$HOME/.venvs/oea/bin/python" -m pip install "./orchestrate_everything_with_agents-3.4.2-py3-none-linux_x86_64.whl[web,mcp,workbench]"
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
.\Install-OEA.ps1 -Wheel .\orchestrate_everything_with_agents-3.4.2-py3-none-win_amd64.whl
.\Start-Workspace.ps1
```

The installer creates the managed runtime, installs the wheel and places `oea` and launchers in `~/.oea/bin`. It does not install a compiler. Source builders may instead provide `OEA_NATIVE_ASSETS`; `-Editable` is an explicit development mode. Stop the host before updating its Python environment.

The launcher starts or reuses the host and opens the browser. New hosts enable configured command/process tools by default, including when opening `Start-Workspace.cmd`. Use `-AllowCommands:$false` in PowerShell to start without that grant. It supports `-Port`, `-NoBrowser`, `-AllowCommands`, `-DataDir` and `-Python`. Reusing a host preserves its existing grant; `host.status.allow_commands` and `agents.check` report it. Changing that grant requires restarting the host after its active work is handled. The launcher does not migrate state, install dependencies or modify models.

<a id="update-tool"></a>
## Update existing data and installations

Protocol 3 uses a new state schema and tool/message/budget contracts. Stop the old host, preserve its source snapshot and inspect the [offline conversion](migration/README.md). Apply to a new target, verify it, then point launchers and clients at that target. Do not copy a live SQLite database or overwrite old state. Existing nonterminal work is retained for review and is not replayed under a different protocol.

Install the matching new platform wheel only after the old runtime is idle. Keep source backups and any new facts when considering rollback.

Continue with the [User Guide](user-guide.md), [中文指南](user-guide.zh-CN.md), [SDK](sdk.md) and [workflows](workflows.md).
