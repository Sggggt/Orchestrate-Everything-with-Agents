# API and transports

All operations are registered by the shared application service. Discover a current operation with `platform_guide` or the registry; use its typed parameters and error contract. The [interface module](interfaces/README.md) documents the CLI, client, control MCP and registry. The [module index](modules.md) links each domain's interface.

```python
from oea import PlatformClient

async def status(data_dir):
    async with PlatformClient.from_data_dir(data_dir) as client:
        return await client.call("host.status", {})
```

The local HTTP gateway serves `/api/protocol`, `/api/rpc`, authenticated events and source-owned artifact downloads. Protocol version is `2.0`; an RPC request includes `protocol_version`, `operation`, `params`, and `request_id`. The response carries a result or structured error. Browser sessions use a local HttpOnly cookie; programmatic clients use the data directory's private token. Only loopback and same-origin requests are accepted.

```sh
oea --version
oea schema agent
oea request host.status
oea mcp
oea migrate --source OLD_STATE_DIRECTORY
```

A request ID correlates transport messages. A domain idempotency key identifies one intended action; retain it when recovering a lost response. Lists are paginated. Large control results use `result_ref` and bounded `results.read`; a reference read never replays a mutation. Current project, consumer and source-version checks remain in force.

See [SDK](sdk.md), [MCP](mcp.md), [workflows](workflows.md), and [migration](migration/README.md) for domain examples.

`results.read` accepts `limit` 1..2048, `offset` 0..2^63-1 and a JSON Pointer of at most 512 characters. Pages identify an immutable `content_version`, the selected JSON text's `content_sha256`, `characters` and `page_limit`. Offsets count Unicode code points. Dynamic responses use saved snapshots; configuration references retain revision checks. An offset beyond the selected text is invalid. Old dynamic references without a content version fail explicitly.

Validation errors expose at most five safe field paths and constraint types without echoing submitted values. Run and workflow resume operations share optional `execution_limits.max_steps` (1..100000), retain the original budget and include the requested adjustment in their idempotency intent.

`runs.response` reads a specific model response without dispatching work. Its first page selects the latest main-model attempt unless `action_id` and `attempt_id` are supplied. Continue using those identities, `version` and `next_offset`; each text page is at most 16384 Unicode code points. Live versions are bounded process-local snapshots. A missing live version fails explicitly; after a host restart, `unsaved_gap` distinguishes unavailable transient text from a retained receipt. A new read may select the original attempt's saved response.

`RunSpec.display` accepts optional `title`, `stage` and `item_label` strings of at most 120 characters. They are display metadata, not instructions or acceptance facts, and do not change the work's idempotency identity; a reused key retains its original display metadata. Workflow Agent nodes set them from node titles and IDs; map nodes can resolve an explicit `item_label` template. Run IDs and original receipts remain available in details.
