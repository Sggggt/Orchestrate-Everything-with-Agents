"""Owned MCP connections. Transport scopes enter and exit in the same task."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit
import os
import time
import httpx
from contextlib import AsyncExitStack
from datetime import timedelta
from dataclasses import dataclass

from .brand import VERSION
from .errors import RuntimeFault, require
from .models import MCPServerConfig


@dataclass
class MCPRequest:
    future: asyncio.Future
    operation_id: str | None = None
    dispatched: bool = False
    replied: bool = False


class MCPConnection:
    """One SDK client, with bounded requests and an explicitly owned lifetime.

    Consumers submit work to this owner instead of entering AnyIO transport
    scopes in short-lived HTTP request tasks. Cancelling one invocation never
    tears down another consumer's shared transport.
    """

    def __init__(self, config: MCPServerConfig, credentials: dict, *, default_cwd=None, recovery_timeout_seconds=30):
        self.config = config.model_copy(deep=True)
        self.credentials = credentials
        self.default_cwd = str(default_cwd) if default_cwd else None
        self.queue = asyncio.Queue(maxsize=128)
        self.slots = asyncio.Semaphore(config.max_concurrency)
        self.task = None
        self.ready = None
        self.children = set()
        self.pending = set()
        self.info = {}
        self.active = 0
        self.last_used = time.monotonic()
        self.closing = False
        self.closed = False
        self.error = None
        self.pins = 0
        self.initialized = False
        self.initialization_lock = asyncio.Lock()
        self.resource_owner = ""
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.operations: dict[str, MCPRequest] = {}

    async def start(self):
        if self.task is None:
            self.ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(self._run(), name=f"mcp:{self.config.id}")
        await asyncio.shield(self.ready)
        require(
            not self.closed and not self.closing,
            "MCP_DISCONNECTED",
            "MCP connection is closed",
        )
        return self

    async def _run(self):
        try:
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
                from mcp.client.streamable_http import streamable_http_client
                from mcp.client.sse import sse_client
                from mcp.types import Implementation
            except ImportError:
                raise RuntimeFault(
                    "MCP_UNAVAILABLE",
                    "Install the platform's mcp dependency group first",
                ) from None
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.config.startup_timeout_seconds):
                    if self.config.transport == "stdio":
                        errlog = stack.enter_context(
                            open(os.devnull, "w", encoding="utf-8")
                        )
                        transport = stdio_client(
                            StdioServerParameters(
                                command=self.config.command,
                                args=list(self.config.args),
                                cwd=self.config.cwd or self.default_cwd,
                                env={
                                    "PYTHONUTF8": "1",
                                    "PYTHONDONTWRITEBYTECODE": "1",
                                    **self.credentials.get("env", {}),
                                },
                            ),
                            errlog=errlog,
                        )
                    elif self.config.transport == "sse":
                        transport = sse_client(
                            self.config.url,
                            headers=self.credentials.get("headers") or None,
                            timeout=self.config.startup_timeout_seconds,
                            sse_read_timeout=self.config.timeout_seconds + self.recovery_timeout_seconds,
                        )
                    else:
                        http = await stack.enter_async_context(
                            httpx.AsyncClient(
                                trust_env=urlsplit(self.config.url).hostname not in {"localhost", "127.0.0.1", "::1"},
                                headers=self.credentials.get("headers") or None,
                                timeout=httpx.Timeout(
                                    self.config.startup_timeout_seconds,
                                    read=max(
                                        self.config.startup_timeout_seconds,
                                        self.config.timeout_seconds + self.recovery_timeout_seconds,
                                    ),
                                ),
                                follow_redirects=False,
                            )
                        )
                        transport = streamable_http_client(
                            self.config.url, http_client=http
                        )
                    streams = await stack.enter_async_context(transport)
                    session = await stack.enter_async_context(
                        ClientSession(
                            streams[0],
                            streams[1],
                            read_timeout_seconds=timedelta(
                                seconds=max(
                                    self.config.startup_timeout_seconds,
                                    self.config.timeout_seconds + self.recovery_timeout_seconds,
                                )
                            ),
                            client_info=Implementation(
                                name="oea", version=VERSION
                            ),
                        )
                    )
                    initialized = await session.initialize()
                    self.info = initialized.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                self.ready.set_result(self.info)
                async with asyncio.TaskGroup() as group:
                    while True:
                        job = await self.queue.get()
                        if job is None:
                            break
                        request, method, params = job
                        future = request.future
                        if future.cancelled():
                            continue
                        task = group.create_task(
                            self._request(session, request, method, params)
                        )
                        self.children.add(task)
                        task.add_done_callback(self.children.discard)
                        future.add_done_callback(
                            lambda f, t=task: t.cancel() if f.cancelled() else None
                        )
        except BaseException as exc:
            self.error = (
                exc
                if isinstance(exc, RuntimeFault)
                else RuntimeFault(
                    "MCP_CONNECT_FAILED",
                    "MCP connection failed; check the executable, address and environment settings",
                    details={"cause": type(exc).__name__},
                )
            )
            if self.ready and not self.ready.done():
                self.ready.set_exception(self.error)
        finally:
            self.closed = True
            error = self.error or RuntimeFault(
                "MCP_DISCONNECTED", "MCP connection has closed"
            )
            for future in self.pending | {request.future for request in self.operations.values()}:
                if not future.done():
                    future.set_exception(error)

    async def _request(self, session, request, method, params):
        future = request.future
        try:
            async with self.slots:
                if future.cancelled():
                    return
                request.dispatched = True
                timeout = self.config.timeout_seconds + (self.recovery_timeout_seconds if request.operation_id else 0)
                async with asyncio.timeout(timeout):
                    if method == "tools/list":
                        value = await session.list_tools(cursor=params.get("cursor"))
                    elif method == "tools/call":
                        value = await session.call_tool(
                            params["name"], params.get("arguments", {})
                        )
                    elif method == "resources/list":
                        value = await session.list_resources(
                            cursor=params.get("cursor")
                        )
                    elif method == "resources/read":
                        value = await session.read_resource(params["uri"])
                    elif method == "prompts/list":
                        value = await session.list_prompts(cursor=params.get("cursor"))
                    elif method == "prompts/get":
                        value = await session.get_prompt(
                            params["name"], params.get("arguments")
                        )
                    elif method == "ping":
                        value = await session.send_ping()
                    else:
                        raise RuntimeFault(
                            "METHOD_NOT_FOUND", "Unsupported MCP client method"
                        )
                request.replied = True
                if not future.done():
                    future.set_result(
                        value.model_dump(mode="json", by_alias=True, exclude_none=True)
                    )
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
        except BaseException as exc:
            if not future.done():
                code = (
                    "MCP_TIMEOUT"
                    if isinstance(exc, TimeoutError)
                    else "MCP_REQUEST_FAILED"
                )
                future.set_exception(
                    exc
                    if isinstance(exc, RuntimeFault)
                    else RuntimeFault(
                        code,
                        "MCP request did not complete successfully",
                        details={"cause": type(exc).__name__},
                    )
                )

    @staticmethod
    def consume_request(future):
        if not future.cancelled():
            future.exception()

    async def request(self, method, params=None, *, operation_id=None):
        await self.start()
        require(self.active < 128, "MCP_QUEUE_FULL", "MCP connection has reached its bounded pending-request limit",
                retryable=True, details={"dispatched": False})
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(self.consume_request)
        request = MCPRequest(future, operation_id)
        if operation_id:
            self.operations[operation_id] = request
        self.pending.add(future)
        self.active += 1
        self.last_used = time.monotonic()
        try:
            self.queue.put_nowait((request, method, params or {}))
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    result = await asyncio.shield(future)
            except TimeoutError:
                raise RuntimeFault("MCP_TIMEOUT", "MCP request exceeded its response deadline",
                                   details={"dispatched": request.dispatched}) from None
            if operation_id:
                self.operations.pop(operation_id, None)
            return result
        except asyncio.QueueFull:
            raise RuntimeFault("MCP_QUEUE_FULL", "MCP connection request queue is full", retryable=True,
                               details={"dispatched": False}) from None
        finally:
            if not operation_id or not request.dispatched:
                if not future.done():
                    future.cancel()
                if operation_id:
                    self.operations.pop(operation_id, None)
            self.active -= 1
            self.last_used = time.monotonic()
            self.pending.discard(future)

    async def aclose(self, *, force=False):
        if self.closed:
            return
        require(
            force or not self.active,
            "RESOURCE_BUSY",
            "Wait for MCP calls to finish before disconnecting",
        )
        self.closing = True
        if force:
            for child in tuple(self.children):
                child.cancel()
        if self.task is None:
            self.closed = True
            return
        await self.queue.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
