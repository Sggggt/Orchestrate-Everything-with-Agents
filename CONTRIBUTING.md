# Contributing

Use Python 3.12 or newer. Keep the virtual environment, test temporary directories and generated output outside the checkout. Create and activate an external environment using the [Windows PowerShell](docs/getting-started.md#windows-powershell), [Windows Command Prompt](docs/getting-started.md#windows-cmd), or [Linux / macOS](docs/getting-started.md#linux-macos) instructions, then install in editable mode from the repository root. Start the platform with `oea serve`.

Use an isolated absolute `OEA_HOME` for development and tests; normal user state lives under `~/.oea/data`. Existing production data is never moved by installing a package or changing this variable. Keep `PYTHONDONTWRITEBYTECODE=1` set when using editable source.

```sh
python -m pip install -c constraints.txt -e ".[web,mcp,workbench]"
python -B -m unittest discover -s tests -p "test_*.py"
node --test tests/test_markdown.mjs tests/test_workflow_graph.mjs tests/test_browser_integration.mjs tests/test_workflow_ui.mjs tests/test_artifacts_ui.mjs tests/test_directory_ui.mjs
python -B scripts/check_release.py
```

Set `TMPDIR`, or Windows `TEMP` and `TMP`, to an external directory. Node is needed for JavaScript tests; browser assets need no bundling. Select relevant tests for a focused change. Generic contract tests use temporary synthetic inputs and protocol stubs, without provider keys or private services.

As an optional Windows convenience alternative, `Install-OEA.ps1 -Editable` prepares the managed external runtime for this checkout, and `Start-Workspace.cmd` selects that runtime automatically. The shared `oea` commands remain the primary documented interface.

Keep SDK, shared operations, CLI/MCP, browser and relevant module documentation consistent. Add tests for observable regressions. Preserve project ownership, revision checks, receipts and unknown-outcome handling. Code modules and matching interface documents are indexed in [docs/modules.md](docs/modules.md).

Do not publish credentials, personal paths, business data, generated inputs or outputs, logs, populated state, captures or development context. Build outside the checkout with `python -B -m build --outdir EXTERNAL_BUILD_DIRECTORY`; inspect the actual wheel and source archive before distribution. Preserve the MIT license and third-party notices.
