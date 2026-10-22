"""Local launchers; outputs and credentials remain outside source directories."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .errors import RuntimeFault
from .models import RuntimeConfig, json_dump
from .paths import default_data_dir, external_path
from .runtime import Runtime
from .secrets import atomic_write


def parser():
    result = argparse.ArgumentParser(prog="oea")
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("serve", "stdio", "mcp", "request", "schema", "backup"):
        child = sub.add_parser(name)
        child.add_argument("--data-dir", default=str(default_data_dir()))
        if name in {"serve", "stdio", "mcp"}:
            child.add_argument("--mock", action="store_true")
            child.add_argument("--reserved-external-slots", type=int, default=None)
        if name == "serve":
            child.add_argument("--port", type=int, default=8765)
            child.add_argument("--allow-trusted-local-execution", action="store_true",
                               help="Allow explicitly configured local command execution (not a system sandbox)")
        elif name == "request":
            child.add_argument("method")
            child.add_argument("--params-file")
            child.add_argument("--output")
        elif name == "mcp":
            child.add_argument("--connect", action="store_true")
            child.add_argument(
                "--compact",
                action="store_true",
                help="Expose only scoped guide/call MCP tools",
            )
        elif name == "schema":
            child.add_argument("--output", required=True)
        elif name == "backup":
            child.add_argument("--output", required=True)
    return result


def load_config(args):
    root = external_path(args.data_dir)
    path = root / "runtime-config.json"
    config = (
        RuntimeConfig.model_validate_json(path.read_text())
        if path.exists()
        else RuntimeConfig()
    )
    if getattr(args, "reserved_external_slots", None) is not None:
        config = RuntimeConfig.model_validate(
            {
                **config.model_dump(),
                "reserved_external_slots": args.reserved_external_slots,
            }
        )
    return config


async def remote_request(root, method, params):
    from urllib.parse import urlsplit

    import httpx

    client = json.loads((root / "client.json").read_text(encoding="utf-8"))
    target = urlsplit(client["url"])
    if (
        target.scheme != "http"
        or target.hostname not in {"127.0.0.1", "localhost", "::1"}
        or target.username
        or target.password
    ):
        raise RuntimeFault(
            "PERMISSION_DENIED",
            "Client metadata must reference a loopback HTTP gateway",
        )
    token = (root / "client-token").read_text(encoding="utf-8").strip()
    async with httpx.AsyncClient(
        timeout=None, follow_redirects=False, trust_env=False
    ) as client_http:
        response = await client_http.post(
            client["url"] + "/api/rpc",
            headers={"Authorization": "Bearer " + token, "X-OEA-View-Version": "1.1"},
            json={"method": method, "params": params},
        )
        data = response.json()
        if "error" in data:
            fault = data["error"]
            raise RuntimeFault(
                fault.get("code", "REMOTE_ERROR"),
                fault.get("safe_message", "Runtime request failed"),
            )
        response.raise_for_status()
        return data


def main():
    args = parser().parse_args()
    root = external_path(args.data_dir)
    try:
        if args.command == "serve":
            import uvicorn

            from .web.app import create_app

            root.mkdir(parents=True, exist_ok=True)
            print(f"Local: http://127.0.0.1:{args.port}", flush=True)
            uvicorn.run(
                create_app(
                    root,
                    load_config(args),
                    mock=args.mock,
                    client_url=f"http://127.0.0.1:{args.port}",
                    allow_trusted_local=args.allow_trusted_local_execution,
                ),
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=25,
            )
        elif args.command == "request":
            params = (
                json.loads(open(args.params_file, encoding="utf-8").read())
                if args.params_file
                else {}
            )
            value = asyncio.run(remote_request(root, args.method, params))
            if args.output:
                target = external_path(args.output)
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, json_dump(value).encode())
            else:
                print(json_dump(value))
        elif args.command == "schema":
            from . import models

            value = {
                name: obj.model_json_schema()
                for name, obj in vars(models).items()
                if isinstance(obj, type)
                and issubclass(obj, models.Model)
                and obj is not models.Model
            }
            path = external_path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json_dump(value).encode())
        elif args.command == "mcp":
            from contextlib import asynccontextmanager

            from agent.mock import configure_mocks

            from .bridges.gateway import gateway_request
            from .bridges.mcp import serve_mcp

            @asynccontextmanager
            async def factory():
                async with Runtime.open(root, load_config(args)) as rt:
                    if args.mock:
                        await configure_mocks(rt)
                    yield rt

            serve_mcp(
                factory,
                compact=args.compact,
                gateway=(
                    lambda operation, params: gateway_request(root, operation, params)
                )
                if args.connect
                else None,
                remote=(
                    (lambda method, params: remote_request(root, method, params))
                    if args.connect
                    else None
                ),
            )
        else:

            async def run():
                async with Runtime.open(root, load_config(args)) as rt:
                    if args.command == "stdio":
                        if args.mock:
                            from agent.mock import configure_mocks

                            await configure_mocks(rt)
                        from .bridges.stdio import serve_stdio

                        await serve_stdio(rt)
                    else:
                        print(json_dump(await rt.backup(args.output)))

            asyncio.run(run())
    except (RuntimeFault, ValueError) as exc:
        value = (
            exc.as_dict()
            if isinstance(exc, RuntimeFault)
            else {
                "code": "INVALID_CONFIGURATION",
                "safe_message": "Invalid configuration",
            }
        )
        print(json_dump({"error": value}), file=sys.stderr)
        raise SystemExit(1)
    except ImportError:
        print(
            "Install the optional dependency group required by this command.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
