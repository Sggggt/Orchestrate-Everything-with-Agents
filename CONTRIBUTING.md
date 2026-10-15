# Contributing

Small, focused changes are welcome. Describe the user-visible problem, the resulting behavior and relevant verification. Keep changes to the SDK, HTTP/MCP surface, Codex skill and browser consistent when adding a platform capability.

## Development setup

Use Python 3.12 or newer. Create a virtual environment and runtime/test outputs outside the checkout, then install from the repository root:

```sh
python -m pip install -c constraints.txt -e '.[web,mcp]'
python -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_markdown.mjs tests/test_workflow_graph.mjs
python -B scripts/check_release.py
```

Node.js is needed only for JavaScript contract tests. The browser application requires no bundling. Python tests use temporary directories and synthetic adapters; they do not require provider keys or private services. Run any visual or manual experiment in a separate external state directory.

## Changes

- Preserve project ownership, version checks, idempotency and uncertain-outcome handling.
- Update the relevant public guide and focused Codex workflow when a contract changes.
- Add tests for behavior that can regress; avoid tests that merely repeat implementation details.
- Keep model keys, tokens, personal paths, company records, downloads, browser captures and generated state out of commits. Use invented generic examples and `example.com` or `example.invalid` for external addresses.
- Keep the shared SVG and existing visual language when changing frontend controls. Verify narrow screens and keyboard access for affected flows.

The source release includes generic automated tests and their source fixtures. It excludes generated test results and populated workspaces. Build packages outside the checkout with `python -m build --outdir /external/path`.

Use the existing MIT license and preserve required copyright notices. Do not include code or data you cannot redistribute.
