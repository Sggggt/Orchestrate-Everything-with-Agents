"""Concurrent JSON-RPC requests and bounded event subscriptions on stdio."""

import asyncio
import json
import sys

from pydantic import ValidationError

from ..errors import RuntimeFault
from ..models import PROTOCOL_VERSION, json_dump
from .dispatch import dispatch


async def serve_stdio(rt):
    initialized = False
    pending = set()
    subscriptions = {}
    writing = asyncio.Lock()
    max_frame = 1024 * 1024

    async def write(frame):
        data = json_dump(frame).encode("utf-8") + b"\n"
        async with writing:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def stream(identity, session_id, after_seq):
        async for event in rt.events.subscribe(session_id, after_seq):
            if event["type"] != "stream.heartbeat":
                await write(
                    {
                        "jsonrpc": "2.0",
                        "method": "runtime.event",
                        "params": {"subscription_id": identity, "event": event},
                    }
                )

    async def handle(frame):
        nonlocal initialized
        identity = frame.get("id")
        try:
            if (
                frame.get("jsonrpc") != "2.0"
                or not isinstance(frame.get("method"), str)
                or not isinstance(frame.get("params", {}), dict)
            ):
                raise RuntimeFault("INVALID_REQUEST", "Invalid JSON-RPC envelope")
            method, params = frame["method"], frame.get("params", {})
            if method == "initialize":
                if str(params.get("protocol_version", "1.0")).split(".")[0] != "1":
                    raise RuntimeFault(
                        "UNSUPPORTED_CAPABILITY",
                        "Wire protocol major version does not match",
                    )
                initialized = True
                value = {
                    "protocol_version": PROTOCOL_VERSION,
                    "max_frame_bytes": max_frame,
                    "capabilities": [
                        "tasks",
                        "messages",
                        "artifacts",
                        "events",
                        "snapshot",
                    ],
                }
            elif not initialized:
                raise RuntimeFault(
                    "NOT_INITIALIZED", "Call initialize before runtime methods"
                )
            elif method == "events.subscribe":
                from ..models import new_id

                session_id = params["session_id"]
                await rt.sessions.get(session_id)
                key = new_id("sub")
                subscriptions[key] = asyncio.create_task(
                    stream(key, session_id, params.get("after_seq", 0))
                )
                value = {"subscription_id": key}
            elif method == "events.unsubscribe":
                task = subscriptions.pop(params["subscription_id"], None)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                value = {"unsubscribed": True}
            else:
                value = await dispatch(rt, method, params)
            if identity is not None:
                await write({"jsonrpc": "2.0", "id": identity, "result": value})
        except (RuntimeFault, ValidationError, KeyError, ValueError) as exc:
            fault = (
                exc
                if isinstance(exc, RuntimeFault)
                else RuntimeFault("INVALID_REQUEST", "Invalid method parameters")
            )
            if identity is not None:
                await write(
                    {
                        "jsonrpc": "2.0",
                        "id": identity,
                        "error": {
                            "code": -32000,
                            "message": fault.safe_message,
                            "data": fault.as_dict(),
                        },
                    }
                )
        except Exception:
            if identity is not None:
                await write(
                    {
                        "jsonrpc": "2.0",
                        "id": identity,
                        "error": {"code": -32603, "message": "Internal bridge error"},
                    }
                )

    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, max_frame + 1)
            if not line:
                break
            try:
                if len(line) > max_frame:
                    raise ValueError
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError
            except (ValueError, UnicodeError):
                await write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": "Invalid or oversized JSONL frame",
                        },
                    }
                )
                continue
            if len(pending) >= 128:
                await write(
                    {
                        "jsonrpc": "2.0",
                        "id": frame.get("id"),
                        "error": {
                            "code": -32000,
                            "message": "Too many pending requests",
                        },
                    }
                )
                continue
            task = asyncio.create_task(handle(frame))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        for task in [*pending, *subscriptions.values()]:
            task.cancel()
        await asyncio.gather(*pending, *subscriptions.values(), return_exceptions=True)
