"""Two compact control tools connected to an existing OEA host."""

from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from oea.interfaces.client import PlatformClient
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode


def create_server(data_dir=None, *, client_factory=None):
    @asynccontextmanager
    async def lifespan(server):
        client = client_factory() if client_factory else PlatformClient.from_data_dir(data_dir)
        async with client:
            yield {"client": client}

    server = FastMCP("oea", instructions="Use one stable consumer_id per conversation. Discover one area/action, then call its shared operation. Read large result references with results.read; do not repeat a write to recover output.",
                     lifespan=lifespan, log_level="WARNING")

    async def call(context, consumer_id, operation, params):
        try:
            require(0 < len(consumer_id) <= 128, "INVALID_REQUEST", "Use one stable conversation consumer identity")
            client = context.request_context.lifespan_context["client"]
            initialized = context.session.client_params
            name = initialized.clientInfo.name if initialized else None
            return await client.call(operation, params, compact=True, consumer_id=consumer_id, agent_name=name)
        except RuntimeFault as error:
            raise ToolError(encode({"error": error.as_dict()})) from None

    @server.tool(name="platform_guide", description="Discover one OEA capability area or one exact action schema. Keep the same consumer_id throughout this conversation; independent conversations use distinct IDs.")
    async def platform_guide(consumer_id: str, area: str = "", action: str | None = None, ctx: Context = None) -> dict:
        return await call(ctx, consumer_id, "host.guide", {"area": area, "action": action})

    @server.tool(name="platform_call", description="Call one operation from its discovered schema. Results and errors share SDK/HTTP semantics. Read a result_ref with results.read and JSON Pointer instead of replaying a write.")
    async def platform_call(consumer_id: str, operation: str, params: dict | None = None, ctx: Context = None) -> dict:
        result = await call(ctx, consumer_id, operation, params or {})
        return result if isinstance(result, dict) else {"value": result}

    return server


def run_server(data_dir=None):
    create_server(data_dir).run(transport="stdio")
