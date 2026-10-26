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
