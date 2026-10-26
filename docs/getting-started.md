# Getting started

Use `oea` as the command-line entry point on Windows, Linux and macOS. Choose the installation for your intended use:

| Use | Installation |
| --- | --- |
| Run the OEA platform and connect MCP clients | [Recommended: uv tool installation](#quick-install) |
| Import `oea` in Python code, or manage an environment yourself | [venv + pip](#sdk-installation) |
| Modify OEA source | [Editable development installation](#development) |
| Use a Windows managed runtime and double-click launcher | [Optional convenience scripts](#windows-shortcuts) |

<a id="quick-install"></a>
## Recommended: install the platform with uv

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) using its instructions for your system, and confirm that `uv --version` works.
2. Download and extract the OEA source archive, or clone the repository. Open a terminal at the directory containing `pyproject.toml` and `constraints.txt`.
3. Run the following commands in Windows PowerShell, Windows Command Prompt, or a Linux/macOS bash or zsh shell:

```sh
uv tool install --python 3.12 -c constraints.txt ".[web,mcp,workbench]"
uv tool update-shell
```

4. Reopen your terminal so PATH changes take effect, then verify and start OEA from any directory:

```sh
oea --version
oea serve
```

uv installs the application in its own persistent tool environment and exposes `oea` on your user's PATH. You do not need to activate a virtual environment for everyday commands. OEA requires Python **3.12+**; `--python 3.12` selects that interpreter, and uv can download it when needed according to its Python-download settings. These commands install from the downloaded source and use its constraints file. See [uv tool installation](https://docs.astral.sh/uv/guides/tools/#installing-tools).

The tool environment is separate from ordinary Python projects: installing the CLI does not make `import oea` available to another Python interpreter. SDK users follow the Python-environment steps below. If several installations provide `oea`, use the [command lookup instructions](#mcp-client) to confirm which executable you are running.

<a id="start"></a>
## Start and inspect the workspace

After the recommended installation, run `oea serve` from any directory. Installation is only needed when preparing or updating the tool. A newly started server runs in the foreground; open its printed URL in a browser and use another terminal for client commands. `Ctrl+C` in the server terminal stops that foreground host. Closing the browser does not stop it. For a manually managed Python environment, activate that environment before running its commands.

The first-launch URL defaults to **http://127.0.0.1:8890**. Later launches retain the saved port. The browser establishes an authenticated session. `oea serve` reuses an authenticated running host for the same data directory. One host owns that directory; clients never open a second host.

```sh
oea --version
oea paths
oea request host.status
oea mcp
```

`oea paths` reports directory locations without creating them. `host.status` and `oea mcp` need an existing running host. The MCP command starts a stdio bridge; an MCP client normally launches it as a subprocess.

Create a project, save an Agent configuration, and open a session. Configure model endpoint, model name and credential locally. Main and manager roles use separate configuration and secrets. Startup registers no demonstration Agents and makes no model calls. Start an idle host with `oea serve --allow-commands` to explicitly enable bound local commands and process Agents; reconnecting does not expand an existing host's permissions.

<a id="sdk-installation"></a>
## Install the SDK in a Python environment

Use these steps to import OEA in your own Python code, or as an alternative if you prefer to manage the platform environment yourself. Install Python **3.12 or newer**, open a terminal at the source repository root, and create an environment outside the checkout. The commands below use a user-owned `.venvs/oea` directory; select the section matching your shell.

<a id="windows-powershell"></a>
### Windows PowerShell

```powershell
python -m venv "$env:USERPROFILE\.venvs\oea"
& "$env:USERPROFILE\.venvs\oea\Scripts\Activate.ps1"
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m pip install -c constraints.txt ".[web,mcp,workbench]"
oea serve
```

If your PowerShell policy prevents activation scripts, invoke the environment's executables directly:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
& "$env:USERPROFILE\.venvs\oea\Scripts\python.exe" -m pip install -c constraints.txt ".[web,mcp,workbench]"
& "$env:USERPROFILE\.venvs\oea\Scripts\oea.exe" serve
```

<a id="windows-cmd"></a>
### Windows Command Prompt (cmd.exe)

```bat
python -m venv "%USERPROFILE%\.venvs\oea"
call "%USERPROFILE%\.venvs\oea\Scripts\activate.bat"
set PYTHONDONTWRITEBYTECODE=1
python -m pip install -c constraints.txt ".[web,mcp,workbench]"
oea serve
```

<a id="linux-macos"></a>
### Linux and macOS (bash / zsh)

The same commands work in these POSIX shells. Ensure `python3` selects Python 3.12+ with the `venv` module available:

```sh
python3 -m venv "$HOME/.venvs/oea"
. "$HOME/.venvs/oea/bin/activate"
export PYTHONDONTWRITEBYTECODE=1
python -m pip install -c constraints.txt ".[web,mcp,workbench]"
oea serve
```

Run SDK scripts with this environment's Python. On later launches of this manually managed platform environment, activate it and run `oea serve`. An editable installation for framework development is described below.

<a id="storage"></a>
## Data and installation locations

The default OEA home is `~/.oea` (`%USERPROFILE%\.oea` on Windows). `oea serve` creates the selected state directory on first use. With uv, the tool runtime and exposed command are managed by uv; with pip, they remain in the Python environment you selected. These installation locations are separate from OEA's persistent state.

| Directory | Contents |
| --- | --- |
| `data/` | Databases, checkpoints, credentials, saved connection information, managed workspaces and artifacts |
| `runtime/` | Python environment created by the optional Windows installer |
| `cache/` | Regenerable caches used by managed installation workflows |
| `bin/` | Command and launchers installed by the optional Windows installer |

These standard managed-installation locations need not all exist with uv or pip. Keep state on local disk outside Git, source and synchronized folders.

`OEA_HOME` is an optional absolute home override for explicit isolation. Advanced `--data-dir` remains available for a selected isolated state. These options change state selection, not the active Python environment. Old `OEA_DATA_DIR` and `OEA_PYTHON` variables no longer silently select normal startup locations. Changing homes does not move existing data. If the retired data-directory setting still points to an existing database and the standard database is absent, normal startup reports `DATA_RELOCATION_REQUIRED` instead of creating an empty replacement. Remove obsolete settings only after a verified relocation; an explicit isolated home remains available for tests.

<a id="mcp-client"></a>
## Make the command available to an MCP client

For a Codex stdio MCP connection, use command `oea` with arguments `["mcp"]`. Start the host first. MCP-created objects are stored by the same OEA host used by the browser and SDK clients, not in the MCP client's own cache or conversation directory. `PlatformClient.from_data_dir()` discovers that same default host.

After `uv tool update-shell`, restart terminals and graphical MCP clients so they see the updated PATH. For a manually managed environment, a graphical client may not inherit its activated PATH. Find the installed executable in a terminal where `oea --version` works:

| Shell | Locate `oea` |
| --- | --- |
| Windows PowerShell | `(Get-Command oea).Source` |
| Windows Command Prompt | `where oea` |
| Linux / macOS | `command -v oea` |

If needed, use the returned absolute executable path as the client's command, with the same `["mcp"]` arguments. Keep host and client pointed at the same OEA home. See [MCP](mcp.md).

<a id="development"></a>
## Editable development installation

After creating and activating an external environment using the instructions for your system, install from the repository root:

```sh
python -m pip install -c constraints.txt -e ".[web,mcp,workbench]"
oea serve
```

Keep `PYTHONDONTWRITEBYTECODE=1` in this environment. Editable mode imports repository code directly. Refresh the browser after frontend changes; restart the idle host after Python changes. Keep test and build output outside the checkout. See [contributing](../CONTRIBUTING.md).

<a id="windows-shortcuts"></a>
## Optional Windows convenience scripts

Windows users who prefer a managed runtime and double-click launcher can run `Install-OEA.cmd`, then `Start-Workspace.cmd` from the source distribution. This is an alternative to preparing a virtual environment manually. Python 3.12+ must be available for installation. The installer prepares `~/.oea/runtime`, installs dependencies and places command entry points in `~/.oea/bin` on the user PATH. Reopen terminals and MCP clients after installation. The same `oea serve` and `oea mcp` commands remain available.

The launcher selects that managed runtime, starts the host in the background or reuses it, and opens the browser. It supports `-Port`, `-NoBrowser`, `-AllowCommands`, and advanced `-DataDir` / `-Python` options. The optional `Install-OEA.ps1 -Editable` variant prepares a managed editable installation. Confirm which environment provides `oea` when multiple installations exist.

## Existing data

For existing data, keep the original host and directory until an offline relocation has been reviewed. Preserve databases, secrets, artifacts, workspaces and absolute path references; do not copy a live database or switch clients to an empty home. The [legacy migration tool](migration/README.md) converts old formats; it is not a same-version directory relocation tool. The launcher does not migrate state, install dependencies or modify model settings. Inspect the dry-run report before applying conversion to a new directory.

<a id="update-tool"></a>
## Update a uv tool installation

Stop the host before updating its environment. Download the intended source version, open its root in a terminal, and reinstall the tool using that source and its constraints:

```sh
uv tool install --reinstall --python 3.12 -c constraints.txt ".[web,mcp,workbench]"
oea --version
oea serve
```

Keep existing runtime state and work directories in place. Updating the tool does not relocate them; a required state upgrade remains a separate offline operation.

Read the [User Guide](user-guide.md) / [中文用户指南](user-guide.zh-CN.md), [SDK](sdk.md), [workflow guide](workflows.md), and [MCP guide](mcp.md) for execution and integration.
