# Contributing

Small, focused changes are welcome. Describe the user-visible problem, the resulting behavior and relevant verification. Keep changes to the SDK, HTTP/MCP surface, Codex skill and browser consistent when adding a platform capability.

## Development setup

Use Python 3.12 or newer. Create a virtual environment and runtime/test outputs outside the checkout, then install from the repository root:

```sh
python -m pip install -c constraints.txt -e '.[web,mcp,langgraph,workbench]'
python -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_markdown.mjs tests/test_workflow_graph.mjs tests/test_agent_status.mjs tests/test_browser_integration.mjs tests/test_message_feed.mjs
python -B scripts/check_release.py
```

Node.js is needed only for JavaScript contract tests. The browser application requires no bundling. Python tests use temporary directories and synthetic adapters; they do not require provider keys or private services. Run any visual or manual experiment in a separate external state directory.

Set `TMPDIR` on Unix, or `TEMP` and `TMP` on Windows, to an external temporary directory before running tests. The development extras above enable the optional LangGraph and image contracts; CI installs them and uses the runner's external temporary directory.

For synthetic concurrency measurements, run `python -B scripts/benchmark_runtime.py --data-dir EXTERNAL_DIRECTORY --mode instances --tasks 512 --agents 4 --concurrency 16 --agent-concurrency 4`. Modes `tasks`, `instances` and `workflow` exercise the same scheduler through different entry points. Reports include throughput, completion percentiles, CPU time and observed concurrency, and assert ordered, complete results and budget compliance. Save reports outside the checkout. These measurements do not represent model-provider throughput.

## Changes

- Preserve project ownership, version checks, idempotency and uncertain-outcome handling.
- Update the relevant public guide and focused Codex workflow when a contract changes.
- Add tests for behavior that can regress; avoid tests that merely repeat implementation details.
- Keep model keys, tokens, personal paths, company records, downloads, browser captures and generated state out of commits. Use invented generic examples and `example.com` or `example.invalid` for external addresses.
- Keep the shared SVG and existing visual language when changing frontend controls. Verify narrow screens and keyboard access for affected flows.

The source release includes generic automated tests and source protocol stubs. Generate test inputs in external temporary directories; do not check in fixed datasets, captures, test reports or populated workspaces. The release check verifies tracked paths and root ignore rules as well as public files and links. Ignoring a file does not remove it from Git. Build packages outside the checkout with `python -B -m build --outdir /external/path`, then inspect the actual wheel and source archive before publishing.

Use the existing MIT license and preserve required copyright notices. Do not include code or data you cannot redistribute.
