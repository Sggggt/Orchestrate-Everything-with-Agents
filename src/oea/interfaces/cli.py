"""The single OEA command entry point."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from oea import __version__
from oea.interfaces.client import PlatformClient
from oea.contracts.configuration import AgentDefinition
from oea.contracts.domain import MessageSpec, RunSpec
from oea.contracts.errors import RuntimeFault


def parser():
    root = argparse.ArgumentParser(prog="oea")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("paths", help="Show the OEA home, data and managed runtime locations")
    schema = commands.add_parser("schema", help="Export a shared configuration or request schema")
    schema.add_argument("model", choices=("agent", "run", "message"), nargs="?", default="agent")
    schema.add_argument("--output", type=Path)
    skill = commands.add_parser("export-skill", help="Export the bundled Codex skill to a new directory")
    skill.add_argument("--output", type=Path, required=True)
    migrate = commands.add_parser("migrate", help="Inspect or convert legacy state offline into a new directory")
    migrate.add_argument("--source", type=Path, required=True)
    migrate.add_argument("--target", type=Path)
    migrate.add_argument("--apply", action="store_true", help="Write a separate new target; the default is a read-only dry run")
    upgrade = commands.add_parser("upgrade-state", help="Inspect or upgrade a recognized v2 state while its host is stopped")
    upgrade.add_argument("--data-dir", type=Path)
    upgrade.add_argument("--backup-file", type=Path)
    upgrade.add_argument("--apply", action="store_true", help="Apply the additive upgrade after creating the specified new backup")
    request = commands.add_parser("request", help="Call one operation on an existing v2 host")
    request.add_argument("operation")
    request.add_argument("--params-file", type=Path)
    request.add_argument("--data-dir", type=Path)
    request.add_argument("--consumer-id")
    request.add_argument("--compact", action="store_true", help="Return references for large operation results")
    mcp = commands.add_parser("mcp", help="Expose the two compact MCP tools through stdio on an existing host")
    mcp.add_argument("--data-dir", type=Path)
    serve = commands.add_parser("serve", help="Start one local host and browser gateway")
    serve.add_argument("--data-dir", type=Path, help="Optional isolated state; normally use the OEA home")
    serve.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    serve.add_argument("--port", type=int, help="Override the saved port (8890 on first launch)")
    serve.add_argument("--allow-commands", action="store_true", help="Enable explicitly bound local command tools with host user permissions")
    return root


async def request(args):
    params = json.loads(args.params_file.read_text(encoding="utf-8")) if args.params_file else {}
    async with PlatformClient.from_data_dir(args.data_dir, consumer_id=args.consumer_id) as client:
        return await client.call(args.operation, params, compact=args.compact)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "paths":
            from oea.workspace.paths import installation_paths
            print(json.dumps(installation_paths(), ensure_ascii=False, indent=2))
        elif args.command == "schema":
            value = {"agent": AgentDefinition, "run": RunSpec, "message": MessageSpec}[args.model].model_json_schema()
            encoded = json.dumps(value, ensure_ascii=False, indent=2)
            if args.output:
                from oea.workspace.paths import external_path
                output = external_path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(encoded + "\n", encoding="utf-8")
            else:
                print(encoded)
        elif args.command == "export-skill":
            from oea.interfaces.skill import export_skill
            print(json.dumps(export_skill(args.output), ensure_ascii=False))
        elif args.command == "migrate":
            from oea.migration.offline import migrate_legacy
            print(json.dumps(migrate_legacy(args.source, target=args.target, apply=args.apply), ensure_ascii=False, indent=2))
        elif args.command == "upgrade-state":
            from oea.migration.upgrade import upgrade_state
            print(json.dumps(upgrade_state(args.data_dir, backup_file=args.backup_file, apply=args.apply), ensure_ascii=False, indent=2))
        elif args.command == "request":
            print(json.dumps(asyncio.run(request(args)), ensure_ascii=False))
        elif args.command == "mcp":
            from oea.interfaces.mcp import run_server
            run_server(args.data_dir)
        else:
            from oea.interfaces.launch import serve
            serve(args.data_dir, host=args.host, port=args.port, allow_commands=args.allow_commands)
        return 0
    except RuntimeFault as error:
        print(json.dumps({"error": error.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, httpx.HTTPError) as error:
        print(json.dumps({"error": {"code": "CLIENT_ERROR", "safe_message": type(error).__name__}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
