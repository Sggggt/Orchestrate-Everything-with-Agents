# API and transports

External application backends use the separately authenticated [OAP 1.0
interface](applications/README.md) at `/api/oap/v1`. The platform control
operations below remain protocol 3.0 and do not expose application business
records. OAP provides fixed business actions, without arbitrary control-operation
forwarding.

All operations are registered by the shared application service. Discover a current operation with `platform_guide` or the registry; use its typed parameters and error contract. The [interface module](interfaces/README.md) documents the CLI, client, control MCP and registry. The [module index](modules.md) links each domain's interface.

```python
from oea import PlatformClient

async def status(data_dir):
    async with PlatformClient.from_data_dir(data_dir) as client:
        return await client.call("host.status", {})
```

The local HTTP gateway serves `/api/protocol`, `/api/rpc`, authenticated events and source-owned artifact downloads. Protocol version is `3.0`; an RPC request includes `protocol_version`, `operation`, `params`, and `request_id`. The response carries a result or structured error. Programmatic clients use the data directory's private token. `POST /api/browser-login` requires management authentication and issues a single-use 60-second ticket. `POST /api/browser-login/redeem` exchanges that ticket for a local HttpOnly cookie. `PlatformClient.browser_url()` and `oea open` handle this flow without putting the control credential in a URL. The public homepage supplies no credential or cookie. Only loopback and same-origin requests are accepted.

```sh
oea --version
oea schema agent
oea request host.status
oea mcp
oea migrate --source OLD_STATE_DIRECTORY
```

A request ID correlates transport messages. A domain idempotency key identifies one intended action; retain it when recovering a lost response. Lists are paginated. Large control results use `result_ref` and bounded `results.read`; a reference read never replays a mutation. Current project, consumer and source-version checks remain in force.

See [SDK](sdk.md), [MCP](mcp.md), [workflows](workflows.md), and [migration](migration/README.md) for domain examples.

`results.read` defaults to 16,384 characters and accepts `limit` 1..65,536, `offset` 0..2^63-1 and a JSON Pointer of at most 512 characters. Pages identify an immutable `content_version`, the selected JSON text's `content_sha256`, `characters` and `page_limit`. Offsets count Unicode code points. Dynamic responses use saved snapshots; configuration references retain revision checks. An offset beyond the selected text is invalid. Old dynamic references without a content version fail explicitly.

Validation errors expose at most five safe field paths and constraint types without echoing submitted values. Run and workflow resume operations share optional `execution_limits.max_steps` (1..100000), retain the original budget and include the requested adjustment in their idempotency intent.

`runs.response` reads a specific model response without dispatching work. Its first page selects the latest main-model attempt unless `action_id` and `attempt_id` are supplied. Continue using those identities, `version` and `next_offset`; pages default to 16,384 Unicode code points with a maximum of 65,536. Live versions are bounded process-local snapshots. A missing live version fails explicitly; after a host restart, `unsaved_gap` distinguishes unavailable transient text from a retained receipt. A new read may select the original attempt's saved response.

`artifacts.candidates` reads file observations from successful patch receipts or, when explicitly selected, the run's output directory. `artifacts.candidate` reads an immutable observation and can verify its current source. Neither operation publishes files, accepts delivery, or starts a model run. See the [artifact boundary](artifacts/README.md) for pagination and live-source limits.

`RunSpec.display` accepts optional `title`, `stage` and `item_label` strings of at most 120 characters. They are display metadata, not instructions or acceptance facts, and do not change the work's idempotency identity; a reused key retains its original display metadata. Workflow Agent nodes set them from node titles and IDs; map nodes can resolve an explicit `item_label` template. Run IDs and original receipts remain available in details.
