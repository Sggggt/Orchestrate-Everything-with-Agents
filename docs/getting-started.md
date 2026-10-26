# Getting started

On Windows, run `Install-OEA.cmd` once from the source distribution, then open `Start-Workspace.cmd`. The installer finds Python 3.12 or newer for bootstrapping, creates OEA's own runtime, installs the declared dependencies, and adds the OEA command directory to your user PATH. Open a new terminal or restart the MCP client after installation so it sees that PATH.

For an existing Python installation on other systems, use an environment outside the checkout:

```sh
python -m pip install -c constraints.txt '.[web,mcp,workbench]'
oea serve
```

The default home is `~/.oea` (`%USERPROFILE%\.oea` on Windows):

| Directory | Contents |
| --- | --- |
| `data/` | Databases, checkpoints, credentials, saved connection information, managed workspaces and artifacts |
| `runtime/` | Windows installer-managed Python environment and application dependencies |
| `cache/` | Regenerable downloads, build and temporary files |
| `bin/` | Installed `oea` entry point and Windows launchers |

Open **http://127.0.0.1:8890** on first launch. Later launches retain the saved port. The browser establishes an authenticated session. `oea serve` reuses an authenticated running host for the same data directory. One host owns that directory; clients never open a second host.

```sh
oea paths
oea request host.status
oea mcp
```

For a Codex stdio MCP connection, use command `oea` with arguments `["mcp"]`. Start the host first. MCP-created objects are stored by the same OEA host used by the browser and SDK clients, not in the MCP client's own cache or conversation directory. `PlatformClient.from_data_dir()` discovers that same default host.

Create a project, save an Agent configuration, and open a session. Configure model endpoint, model name and credential locally. Main and manager roles use separate configuration and secrets. Startup registers no demonstration Agents and makes no model calls. `--allow-commands` explicitly enables bound local commands and process Agents; reconnecting does not expand an existing host's permissions.

For development, run `Install-OEA.ps1 -Editable` or install with `pip install -e '.[web,mcp,workbench]'` in an external environment. Editable mode imports repository code directly. Refresh the browser after frontend changes; restart the idle host after Python changes. Keep test and build output outside the checkout.

`OEA_HOME` is an optional absolute home override for explicit isolation. Advanced `--data-dir` and launcher `-DataDir` remain available for selected isolated states. The Windows launcher also supports `-Python`, `-Port`, `-NoBrowser`, and `-AllowCommands`; normal use needs none of the directory or interpreter options. It selects the managed runtime automatically. Old `OEA_DATA_DIR` and `OEA_PYTHON` variables no longer silently select normal startup locations. Changing homes does not move existing data. If the retired data-directory setting still points to an existing database and the standard database is absent, normal startup reports `DATA_RELOCATION_REQUIRED` instead of creating an empty replacement. Remove obsolete settings only after a verified relocation; an explicit isolated home remains available for tests.

For existing data, keep the original host and directory until an offline relocation has been reviewed. Preserve databases, secrets, artifacts, workspaces and absolute path references; do not copy a live database or switch clients to an empty home. The [legacy migration tool](migration/README.md) converts old formats; it is not a same-version directory relocation tool. The launcher does not migrate state, install dependencies or modify model settings. Inspect the dry-run report before applying conversion to a new directory.

Read the [SDK](sdk.md), [workflow guide](workflows.md), and [MCP guide](mcp.md) for execution and integration.
