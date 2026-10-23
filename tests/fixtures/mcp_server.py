"""Deterministic local MCP fixture; never accesses external systems."""

import asyncio
import os
import argparse
import time
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations, CallToolResult, TextContent

server = FastMCP(
    "Platform contract fixture",
    instructions="Select the fixture project before reading project-scoped data.",
)
selected = "default"
counter = 0


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def structured_failure() -> CallToolResult:
    """Return a safe structured upstream rejection for error propagation contracts."""
    error = {"code": "SAMPLE_REJECTED", "message": "A bounded query is required",
             "path": ["query", "where"], "issues": [{"expected": "bounded"}]}
    return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(error))], structuredContent=error)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> dict:
    """Return text and the local fixture process identity."""
    return {"text": text, "pid": os.getpid(), "project": selected}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def select_project(project: str) -> dict:
    """Select state inside this fixture process only."""
    global selected
    selected = project
    return {"project": selected}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def increment() -> dict:
    """Increment a process-local counter once."""
    global counter
    counter += 1
    return {"counter": counter}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
async def timed_increment(delay: float = .1) -> dict:
    """Return the actual completion of one delayed process-local mutation."""
    global counter
    await asyncio.sleep(min(delay, 3))
    counter += 1
    return {"counter": counter}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def delay_echo(text: str, delay: float = 0.01) -> dict:
    """Exercise concurrent request routing and cancellation."""
    await asyncio.sleep(min(delay, 3))
    return {"text": text}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def timed_echo(delay: float = 0.1) -> dict:
    """Expose actual call overlap and connection state for capacity contracts."""
    started = time.monotonic_ns()
    await asyncio.sleep(min(delay, 3))
    return {"started": started, "ended": time.monotonic_ns(), "project": selected, "pid": os.getpid()}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def credential_echo(password: str = "") -> dict:
    """Exercise schema preservation and configured-secret redaction."""
    return {"value": os.environ.get("FIXTURE_SECRET", ""), "password": password}


@server.resource("fixture://readme")
def readme() -> str:
    return "A local MCP resource used only for platform contract tests."


@server.resource("fixture://timed/{delay}")
async def timed_resource(delay: str) -> str:
    started = time.monotonic_ns()
    await asyncio.sleep(min(float(delay), 3))
    return json.dumps({"started": started, "ended": time.monotonic_ns()})


@server.prompt()
def review_request(topic: str) -> str:
    return "Review the following fixture topic: " + topic


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "streamable-http", "sse"]
    )
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args()
    server.settings.host = "127.0.0.1"
    server.settings.port = options.port
    server.run(transport=options.transport)
