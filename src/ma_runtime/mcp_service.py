"""Persistent MCP registry, isolated/shared clients and mediated tool access."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tomllib
from collections import OrderedDict
from pathlib import Path

import jsonschema

from .errors import RuntimeFault, require
from .mcp_client import MCPConnection
from .models import MCPServerConfig, QuantityRequirement, content_hash, json_dump, new_id, now_ms
from .store import one, rows
from .tools import ToolSpec
from .paths import safe_child


def tool_alias(server_id, name):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:24]
    digest = hashlib.sha256(f"{server_id}:{name}".encode()).hexdigest()[:10]
    return f"mcp_{server_id[:20]}_{safe}_{digest}"


class MCPToolProvider:
    def __init__(self, registry, server_id, tool):
        self.registry, self.server_id, self.tool = registry, server_id, tool

    def describe(self):
        config = self.registry.configs[self.server_id]
        return ToolSpec(
            name=tool_alias(self.server_id, self.tool["name"]),
            input_schema=self.tool.get("inputSchema") or {"type": "object"},
            output_schema={"type": "object"},
            side_effects="NONE"
            if self.tool.get("annotations", {}).get("readOnlyHint") is True
            else "EXTERNAL",
            timeout_seconds=config.timeout_seconds + config.startup_timeout_seconds + 5,
            supports_idempotency=True,
        )

    async def invoke(self, request):
        value = await self.registry.call_for_run(
            request["caller_run_id"],
            self.server_id,
            self.tool["name"],
            request["arguments"],
            request["idempotency_key"],
        )
        return self.registry.bounded_result(
            value, self.registry.rt.config.max_message_bytes - 2048
        )

    async def reconcile(self, operation_ref):
        raise RuntimeFault(
            "UNSUPPORTED_CAPABILITY", "This MCP server has no outcome probe contract"
        )


class MCPRegistry:
    def __init__(self, runtime):
        self.rt = runtime
        self.configs = {}
        self.catalogs = {}
        self.credentials = {}
        self.connections = {}
        self.errors = {}
        self.lock = asyncio.Lock()
        self.result_cache = OrderedDict()
        self.result_cache_bytes = 0
        self.closing = False
        self.reconfiguring = set()
        from .mcp_resources import MCPResources
        self.capacity = MCPResources(self)

    async def open(self):
        for row in await self.rt.store.call(
            lambda db: rows(db, "SELECT * FROM mcp_servers ORDER BY created_at,id")
        ):
            self.configs[row.id] = MCPServerConfig.model_validate_json(row.config_json)
            self.catalogs[row.id] = json.loads(row.catalog_json)
            self.credentials[row.id] = json.loads(row.credentials_json)
        await self.rt.store.call(
            lambda db: db.execute(
                "UPDATE mcp_calls SET state='UNCERTAIN',error_json=?,ended_at=? WHERE state='DISPATCHED'",
                (
                    json_dump(
                        {
                            "code": "OUTCOME_UNCERTAIN",
                            "safe_message": "Previous MCP invocation requires external outcome verification",
                        }
                    ),
                    now_ms(),
                ),
            ),
            transaction=True,
        )
        await self.capacity.open()
        self.refresh_providers()

    async def configure_policy(self, server_id, policy):
        return await self.capacity.configure(server_id, policy)

    async def policy(self, server_id):
        return await self.capacity.inspect(server_id)

    async def reconcile_capacity(self, call_id, *, confirmed_stopped, evidence):
        return await self.capacity.reconcile(call_id, confirmed_stopped=confirmed_stopped, evidence=evidence)

    def _config(self, server_id):
        config = self.configs.get(server_id)
        require(config is not None, "NOT_FOUND", "MCP server is not registered")
        return config

    @staticmethod
    def bounded_result(value, limit):
        encoded = json_dump(value).encode("utf-8")
        if len(encoded) <= limit:
            return value
        preview = encoded[: max(128, limit // 3)].decode("utf-8", errors="ignore")
        return {
            **{
                key: value[key]
                for key in ("call_id", "server_id", "tool_name", "isError", "reused")
                if key in value
            },
            "content": [
                {
                    "type": "text",
                    "text": "Result truncated to the configured context limit. Narrow the query to retrieve a smaller result.\n"
                    + preview,
                }
            ],
            "truncated": True,
            "original_bytes": len(encoded),
            "untrusted_tool_result": True,
        }

    def _catalog_redact(self, value):
        # Schema property names such as "password" are not credential values.
        if isinstance(value, str):
            return self.rt.vault.redact(value)
        if isinstance(value, dict):
            return {k: self._catalog_redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._catalog_redact(v) for v in value]
        return value

    def _public(self, config):
        clients = [
            c
            for (sid, _), c in self.connections.items()
            if sid == config.id and not c.closed
        ]
        connected = [
            c
            for c in clients
            if c.ready
            and c.ready.done()
            and not c.ready.cancelled()
            and not c.ready.exception()
        ]
        catalog = self.catalogs.get(config.id, {})
        return {
            **config.model_dump(mode="json"),
            "status": "DISABLED"
            if not config.enabled
            else "CONNECTED"
            if connected
            else "CONNECTING"
            if clients
            else "ERROR"
            if config.id in self.errors
            else "DISCONNECTED",
            "connections": len(connected),
            "active_calls": sum(c.active for c in clients),
            "tool_count": len(catalog.get("tools", [])),
            "server_info": catalog.get("serverInfo", {}),
            "capabilities": catalog.get("capabilities", {}),
            "discovered_at": catalog.get("discovered_at"),
            "env_keys": list(self.credentials.get(config.id, {}).get("env", {})),
            "header_keys": list(self.credentials.get(config.id, {}).get("headers", {})),
            "last_error": self.errors.get(config.id),
            "bound_agents": [
                a.id
                for a in self.rt.agent_configs.values()
                if any(b.server_id == config.id for b in a.mcp_bindings)
            ],
        }

    async def list(self):
        return [self._public(c) for c in self.configs.values()]

    async def get(self, server_id):
        return {
            **self._public(self._config(server_id)),
            "catalog": self.catalogs.get(server_id, {}),
        }

    async def register_directory(self, directory):
        root = Path(directory).expanduser()
        require(root.is_absolute() and root.is_dir(), "INVALID_REQUEST", "Choose an existing absolute MCP directory")
        root = root.resolve()
        source = root / "mcp.json"
        require(source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(root)
                and source.stat().st_size <= 65536, "INVALID_REQUEST", "MCP directory requires mcp.json within 64 KiB")
        try:
            config = json.loads(await asyncio.to_thread(source.read_text, encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            raise RuntimeFault("INVALID_REQUEST", "Cannot read MCP directory configuration") from None
        require(isinstance(config, dict) and not ({"secret_values", "env", "headers"} & set(config)),
                "INVALID_REQUEST", "mcp.json must contain public MCPServerConfig fields; save credentials separately")
        config["source_directory"] = str(root)
        if config.get("transport", "stdio") == "stdio":
            config["cwd"] = str((root / config.get("cwd", ".")).resolve())
            if config.get("command", "").startswith(("./", ".\\")):
                config["command"] = str((root / config["command"]).resolve())
        parsed = MCPServerConfig.model_validate(config)
        previous = self.configs.get(parsed.id)
        require(previous is None or previous.source_directory == str(root),
                "MCP_CONFLICT", "MCP ID is registered from another source")
        return await self.configure(parsed)

    async def configure(
        self, config: MCPServerConfig | dict, secret_values: dict | None = None
    ):
        self.rt.admit()
        config = MCPServerConfig.model_validate(config).model_copy(deep=True)
        require(self.capacity.policy(config.id).reserved_connections < config.max_connections
                if config.id in self.configs else True, "INVALID_REQUEST", "Connection limit must exceed its configured reservation")
        require(
            not config.url or not self._is_own_gateway(config.url),
            "INVALID_REQUEST",
            "A managed MCP server cannot point back to this platform",
        )
        if secret_values is not None:
            require(
                isinstance(secret_values, dict)
                and set(secret_values) <= {"env", "headers"},
                "INVALID_REQUEST",
                "Secret values must contain env and/or headers maps",
            )
            for group, values in secret_values.items():
                require(
                    isinstance(values, dict) and len(values) <= 64,
                    "INVALID_REQUEST",
                    "Credential settings must be a bounded string map",
                )
                for key, value in values.items():
                    require(
                        isinstance(key, str)
                        and isinstance(value, str)
                        and len(value) <= 16384
                        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", key),
                        "INVALID_REQUEST",
                        "Invalid environment or header entry",
                    )
                    require(
                        "\r" not in value and "\n" not in value
                        if group == "headers"
                        else "\0" not in value,
                        "INVALID_REQUEST",
                        "Invalid credential value",
                    )
        async with self.lock:
            clients = [
                c for (sid, _), c in self.connections.items() if sid == config.id
            ]
            require(
                not any(c.active or c.pins for c in clients) and not any(
                    owner.startswith("mcp:connection:" + config.id + ":") for owner in self.capacity.uncertain_connections),
                "RESOURCE_BUSY",
                "Wait for active MCP calls before changing this server",
            )
            await asyncio.gather(*(c.aclose() for c in clients))
            for client in clients:
                self.rt.resources.release(client.resource_owner)
            self.connections = {
                k: v for k, v in self.connections.items() if k[0] != config.id
            }
            old_credentials = self.credentials.get(config.id, {})
            credentials = {k: dict(v) for k, v in old_credentials.items()}
            if secret_values is not None:
                for group, values in secret_values.items():
                    credentials[group] = {}
                    for key, value in values.items():
                        reference = f"mcp:{config.id}:{new_id('secret')}"
                        await asyncio.to_thread(self.rt.vault.set, reference, value)
                        credentials[group][key] = reference
            stamp = now_ms()
            await self.rt.store.call(
                lambda db: db.execute(
                    "INSERT INTO mcp_servers(id,config_json,catalog_json,credentials_json,created_at,updated_at) VALUES(?,?,'{}',?,?,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,catalog_json='{}',credentials_json=excluded.credentials_json,revision=revision+1,updated_at=excluded.updated_at",
                    (
                        config.id,
                        json_dump(config),
                        json_dump(credentials),
                        stamp,
                        stamp,
                    ),
                ),
                transaction=True,
            )
            self.configs[config.id] = config
            self.catalogs[config.id] = {}
            self.credentials[config.id] = credentials
            self.errors.pop(config.id, None)
            used = {ref for group in credentials.values() for ref in group.values()}
            for group in old_credentials.values():
                for ref in group.values():
                    if ref not in used:
                        await asyncio.to_thread(self.rt.vault.set, ref, "")
            self.refresh_providers()
        return await self.get(config.id)

    def _is_own_gateway(self, url):
        client = self.rt.root / "client.json"
        if not client.exists():
            return False
        try:
            return url.rstrip("/").startswith(
                json.loads(client.read_text(encoding="utf-8"))["url"].rstrip("/")
                + "/api/"
            )
        except (ValueError, KeyError):
            return False

    async def delete(self, server_id):
        self.rt.admit()
        self._config(server_id)
        require(not any(owner.startswith("mcp:connection:" + server_id + ":") for owner in self.capacity.uncertain_connections),
                "RESOURCE_BUSY", "Reconcile unresolved MCP occupancy before deleting its server")
        require(
            not any(
                any(b.server_id == server_id for b in a.mcp_bindings)
                for a in self.rt.agent_configs.values()
            ),
            "RESOURCE_BUSY",
            "Remove this server's Agent bindings before deleting it",
        )
        await self.disconnect(server_id)
        async with self.lock:
            await self.rt.store.call(
                lambda db: db.execute(
                    "DELETE FROM mcp_servers WHERE id=?", (server_id,)
                ),
                transaction=True,
            )
            for group in self.credentials.pop(server_id, {}).values():
                for ref in group.values():
                    await asyncio.to_thread(self.rt.vault.set, ref, "")
            self.configs.pop(server_id, None)
            self.catalogs.pop(server_id, None)
            self.errors.pop(server_id, None)
            self.capacity.policies.pop(server_id, None)
            self.refresh_providers()
        return {"deleted": True}

    async def disconnect(self, server_id):
        self._config(server_id)
        async with self.lock:
            clients = [
                c for (sid, _), c in self.connections.items() if sid == server_id
            ]
            require(
                not any(c.active or c.pins for c in clients),
                "RESOURCE_BUSY",
                "MCP calls are still running",
            )
            for client in clients:
                client.closing = True
        await asyncio.gather(*(asyncio.shield(self.capacity.closures[c.resource_owner]) if c.resource_owner in self.capacity.closures
                               else c.aclose() for c in clients))
        async with self.lock:
            self.connections = {
                k: c for k, c in self.connections.items() if c not in clients
            }
            self.errors.pop(server_id, None)
        for client in clients:
            await self.capacity.connection_closed(client)
        return await self.get(server_id)

    def _consumer(self, consumer_id):
        require(
            isinstance(consumer_id, str)
            and re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", consumer_id),
            "INVALID_REQUEST",
            "Use a stable consumer ID with letters, digits or ._:@/-",
        )
        return "external:" + consumer_id

    async def _connection(self, server_id, consumer):
        self.rt.admit()
        require(not self.closing, "RUNTIME_CLOSING", "MCP manager is closing")
        config = self._config(server_id)
        require(config.enabled, "MCP_DISABLED", "This MCP server is disabled")
        require(server_id not in self.reconfiguring, "MCP_CONFIG_CHANGED", "MCP connection policy is being changed", details={"dispatched": False})
        key = (server_id, "shared" if config.session_scope == "shared" else consumer)
        _, connection_key, owner_key = self.capacity.define(config, consumer)
        require(not any(held == owner_key or held.startswith(owner_key + ":") for held in self.capacity.uncertain_connections)
                or (key in self.connections and not self.connections[key].closed),
                "OUTCOME_UNCERTAIN", "Previous calls on this consumer require stop confirmation before rebuilding its connection")
        owner = None
        try:
            async with asyncio.timeout(config.startup_timeout_seconds):
                while True:
                    self.rt.resources.changed.clear()
                    async with self.lock:
                        require(self._config(server_id) == config and config.enabled and server_id not in self.reconfiguring, "MCP_CONFIG_CHANGED", "MCP configuration changed during connection admission")
                        client = self.connections.get(key)
                        if client is not None and not client.closed and not client.closing:
                            client.pins += 1
                            break
                    if owner is None:
                        owner = owner_key + ":" + new_id("generation")
                    claim = (QuantityRequirement(key=connection_key),)
                    await self.capacity.reclaim(server_id, pressure=not self.rt.resources.can_acquire(owner, (), claim))
                    async with self.lock:
                        require(self._config(server_id) == config and config.enabled and server_id not in self.reconfiguring, "MCP_CONFIG_CHANGED", "MCP configuration changed during connection admission")
                        client = self.connections.get(key)
                        if client is not None and not client.closed and not client.closing:
                            client.pins += 1
                            break
                        if client is None and self.rt.resources.acquire(owner, (), claim):
                            credentials = {group: {name: self.rt.vault.get(ref) for name, ref in values.items()}
                                           for group, values in self.credentials.get(server_id, {}).items()}
                            workspace = safe_child(self.rt.root, f"mcp-workspaces/{server_id}/{hashlib.sha256(key[1].encode()).hexdigest()[:16]}")
                            workspace.mkdir(parents=True, exist_ok=True)
                            client = MCPConnection(config, credentials, default_cwd=workspace,
                                recovery_timeout_seconds=self.capacity.policy(server_id).recovery_timeout_seconds)
                            client.resource_owner = owner
                            client.pins = 1
                            self.connections[key] = client
                            break
                    await self.rt.resources.changed.wait()
        except TimeoutError:
            raise RuntimeFault("MCP_CAPACITY", "MCP connection capacity did not become available within the bounded wait",
                               retryable=True, details={"dispatched": False}) from None
        try:
            await client.start()
            self.errors.pop(server_id, None)
        except RuntimeFault as exc:
            self.errors[server_id] = exc.as_dict()
            raise
        finally:
            client.pins -= 1
            self.rt.resources.changed.set()
        return client

    async def connect(self, server_id, consumer_id="console"):
        consumer = self._consumer(consumer_id)
        async with self.capacity.borrow(server_id, consumer) as client:
            await self._discover(server_id, client)
        return await self.get(server_id)

    async def _discover(self, server_id, client, *, publish=True):
        collected, cursor, seen = [], None, set()
        if client.info.get("capabilities", {}).get("tools") is not None:
            for _ in range(32):
                page = await client.request("tools/list", {"cursor": cursor})
                for tool in page.get("tools", []):
                    require(
                        isinstance(tool.get("name"), str)
                        and tool["name"] not in {t["name"] for t in collected},
                        "MCP_PROTOCOL_ERROR",
                        "MCP tool names must be unique",
                    )
                    tool["alias"] = tool_alias(server_id, tool["name"])
                    try:
                        jsonschema.Draft202012Validator.check_schema(
                            tool.get("inputSchema", {"type": "object"})
                        )
                    except jsonschema.SchemaError:
                        raise RuntimeFault(
                            "MCP_PROTOCOL_ERROR",
                            "MCP server returned an invalid tool schema",
                        ) from None
                    collected.append(tool)
                require(
                    len(collected) <= 512,
                    "MCP_CATALOG_LIMIT",
                    "MCP server exposes too many tools",
                )
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                require(
                    cursor not in seen,
                    "MCP_PROTOCOL_ERROR",
                    "MCP pagination cursor repeated",
                )
                seen.add(cursor)
            else:
                raise RuntimeFault(
                    "MCP_CATALOG_LIMIT", "MCP tool pagination exceeded its limit"
                )
        catalog = self._catalog_redact(
            {**client.info, "tools": collected, "discovered_at": now_ms()}
        )
        require(
            len(json_dump(catalog).encode()) <= 2097152,
            "MCP_CATALOG_LIMIT",
            "MCP tool catalog exceeds 2 MiB",
        )
        client.catalog = catalog
        client.schema_fingerprint = content_hash(collected)
        if not publish:
            return catalog
        async with self.lock:
            require(
                self.configs.get(server_id) == client.config,
                "MCP_CONFIG_CHANGED",
                "MCP configuration changed during discovery; reconnect",
            )
            self.catalogs[server_id] = catalog
            await self.rt.store.call(
                lambda db: db.execute(
                    "UPDATE mcp_servers SET catalog_json=?,updated_at=? WHERE id=?",
                    (json_dump(catalog), now_ms(), server_id),
                ),
                transaction=True,
            )
            self.refresh_providers()
        return catalog

    async def tools(self, server_id, refresh=False, consumer_id="console"):
        config = self._config(server_id)
        if refresh or not self.catalogs.get(server_id, {}).get("discovered_at"):
            await self.connect(server_id, consumer_id)
        return {
            "server_id": server_id,
            "tools": [
                t
                for t in self.catalogs.get(server_id, {}).get("tools", [])
                if config.allowed_tools is None or t["name"] in config.allowed_tools
            ],
        }

    def tool_summary(self, server_id, tool):
        from .models import content_hash
        schema = tool.get("inputSchema") or {"type": "object"}
        size = len(json_dump(schema).encode("utf-8"))
        catalog = self.catalogs.get(server_id, {})
        return {"name": tool["name"], "identifier": f"mcp:{server_id}:{tool['name']}",
                "description": tool.get("description", "")[:600],
                "protocol_version": catalog.get("protocolVersion"), "discovered_at": catalog.get("discovered_at"),
                "fingerprint": content_hash({"protocol_version": catalog.get("protocolVersion"), "tool": tool}),
                "schema_bytes": size, "schema_tokens_estimate": (size + 2) // 3}

    async def catalog(self, server_id, cursor=None, limit=25):
        config = self._config(server_id)
        catalog = self.catalogs.get(server_id, {})
        limit = max(1, min(int(limit), 100))
        tools = sorted((t for t in catalog.get("tools", []) if (not cursor or t["name"] > cursor)
                        and (config.allowed_tools is None or t["name"] in config.allowed_tools)), key=lambda t: t["name"])[:limit + 1]
        return {"server_id": server_id, "enabled": config.enabled, "source": "cached" if catalog.get("discovered_at") else "unknown",
                "items": [self.tool_summary(server_id, t) for t in tools[:limit]],
                "next_cursor": tools[limit - 1]["name"] if len(tools) > limit else None}

    async def tool(self, server_id, tool_name, expected_fingerprint=None):
        config = self._config(server_id)
        require(config.enabled and (config.allowed_tools is None or tool_name in config.allowed_tools),
                "PERMISSION_DENIED", "Tool is disabled or unavailable")
        catalog = self.catalogs.get(server_id, {})
        require(catalog.get("discovered_at"), "MCP_CATALOG_UNAVAILABLE", "Discover this server before reading a tool schema")
        tool = next((t for t in catalog.get("tools", []) if t["name"] == tool_name), None)
        require(tool is not None, "NOT_FOUND", "Tool is absent from the cached catalog")
        summary = self.tool_summary(server_id, tool)
        require(expected_fingerprint is None or expected_fingerprint == summary["fingerprint"],
                "REVISION_CONFLICT", "Tool schema changed; read its current directory entry")
        return {**summary, "tool": tool, "validation": {"local_json_schema": "valid_at_discovery",
                "oea_model_input": "depends_on_selected_catalog_and_context", "provider_tool_support": "unknown", "upstream_execution": "not_probed"}}

    def refresh_providers(self):
        for name, provider in tuple(self.rt.tools.providers.items()):
            if isinstance(provider, MCPToolProvider):
                self.rt.tools.providers.pop(name, None)
                self.rt.tools.allowed.pop(name, None)
                self.rt.tools.validators.pop(name, None)
        for server_id, catalog in self.catalogs.items():
            config = self.configs.get(server_id)
            if not config or not config.enabled:
                continue
            for tool in catalog.get("tools", []):
                if (
                    config.allowed_tools is not None
                    and tool["name"] not in config.allowed_tools
                ):
                    continue
                allowed = [
                    a.id
                    for a in self.rt.agent_configs.values()
                    if a.enabled
                    and any(
                        b.server_id == server_id
                        and (b.tools is None or tool["name"] in b.tools)
                        for b in a.mcp_bindings
                    )
                ]
                self.rt.tools.register(
                    MCPToolProvider(self, server_id, tool), allowed_agent_ids=allowed
                )

    def validate_bindings(self, bindings):
        for binding in bindings:
            self._config(binding.server_id)

    async def schemas_for_run(self, run_id):
        from .models import TaskSpec
        from .execution_policy import mcp_name
        task, run = await self.rt.store.call(lambda db: self.rt.check_run(db, run_id))
        policy = TaskSpec.model_validate_json(task.spec_json).tool_policy
        bindings = (
            json.loads(run.adapter_run_ref_json or "{}")
            .get("scopes", {})
            .get("mcp_bindings", [])
        )
        tools, instructions, unavailable, identifiers = [], [], [], {}
        for binding in bindings:
            sid = binding["server_id"]
            if policy.mode == "none" or (policy.allowed is not None and not any(name.startswith(f"mcp:{sid}:") for name in policy.allowed)):
                continue
            try:
                require(self._config(sid).enabled, "MCP_DISABLED", "Required MCP server is disabled")
                for initial in self.capacity.policy(sid).initialization:
                    await self.authorize_run(run_id, sid, initial.tool_name)
                if not self.catalogs.get(sid, {}).get("discovered_at"):
                    await self.connect(sid, "catalog")
                requested = {name.split(":", 2)[2] for name in (policy.allowed or ()) + policy.required
                             if name.startswith(f"mcp:{sid}:")}
                require(requested <= {tool["name"] for tool in self.catalogs[sid].get("tools", [])},
                        "MCP_TOOL_POLICY_INVALID", "Task policy selects an unknown MCP tool")
                for tool in self.catalogs[sid].get("tools", []):
                    if not policy.permits(mcp_name(sid, tool["name"])):
                        continue
                    if (
                        binding.get("tools") is not None
                        and tool["name"] not in binding["tools"]
                    ):
                        continue
                    config = self.configs[sid]
                    if (
                        config.allowed_tools is not None
                        and tool["name"] not in config.allowed_tools
                    ):
                        continue
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["alias"],
                                "description": f"[{config.name}; {mcp_name(sid, tool['name'])}] "
                                + tool.get("description", "")[:6000],
                                "parameters": tool.get("inputSchema")
                                or {"type": "object"},
                            },
                        }
                    )
                    identifiers[tool["alias"]] = mcp_name(sid, tool["name"])
                if self.catalogs[sid].get("instructions"):
                    instructions.append(
                        {
                            "server_id": sid,
                            "instructions": self.catalogs[sid]["instructions"][:16000],
                        }
                    )
            except RuntimeFault as exc:
                if exc.code == "MCP_TOOL_POLICY_INVALID":
                    raise
                unavailable.append({"server_id": sid, "error": exc.as_dict()})
        require(
            len(tools) <= 96,
            "MCP_TOOL_LIMIT",
            "Select at most 96 MCP tools for one Agent",
        )
        return {
            "tools": tools,
            "instructions": instructions,
            "unavailable": unavailable,
            "identifiers": identifiers,
        }

    async def call_for_run(
        self, run_id, server_id, tool_name, arguments, idempotency_key
    ):
        task = await self.authorize_run(run_id, server_id, tool_name)
        return await self._call(server_id, tool_name, arguments, "thread:" + task.thread_id,
                                idempotency_key, run_id=run_id)

    async def authorize_run(self, run_id, server_id, tool_name):
        task, run = await self.rt.store.call(lambda db: self.rt.check_run(db, run_id))
        from .models import TaskSpec
        from .execution_policy import mcp_name
        require(TaskSpec.model_validate_json(task.spec_json).tool_policy.permits(mcp_name(server_id, tool_name)),
                "PERMISSION_DENIED", "Tool is disabled by this task's policy")
        scopes = json.loads(run.adapter_run_ref_json or "{}").get("scopes", {})
        require(
            any(
                b["server_id"] == server_id
                and (b.get("tools") is None or tool_name in b["tools"])
                for b in scopes.get("mcp_bindings", [])
            ),
            "PERMISSION_DENIED",
            "MCP tool was not granted to this execution",
        )
        spec = json.loads(task.spec_json)
        current = self.rt.agent_configs.get(spec["agent_id"])
        require(
            current
            and current.enabled
            and any(
                b.server_id == server_id and (b.tools is None or tool_name in b.tools)
                for b in current.mcp_bindings
            ),
            "PERMISSION_DENIED",
            "This Agent's MCP tool access has been revoked",
        )
        return task

    async def call(
        self,
        server_id,
        tool_name,
        arguments=None,
        consumer_id="codex",
        idempotency_key=None,
    ):
        require(
            self._config(server_id).expose_to_codex,
            "PERMISSION_DENIED",
            "This server is not exposed to external platform clients",
        )
        return await self._call(
            server_id,
            tool_name,
            arguments or {},
            self._consumer(consumer_id),
            idempotency_key or new_id("request"),
        )

    async def _call(self, server_id, tool_name, arguments, consumer, key, run_id=None):
        require(isinstance(arguments, dict) and len(json_dump(arguments).encode()) <= 262144,
                "INVALID_REQUEST", "MCP arguments must be a JSON object within 256 KiB")
        require(isinstance(key, str) and 0 < len(key) <= 256, "INVALID_REQUEST", "A bounded idempotency key is required")
        digest = content_hash({"tool": tool_name, "arguments": arguments})
        previous = await self.rt.store.call(lambda db: one(db,
            "SELECT * FROM mcp_calls WHERE server_id=? AND consumer_id=? AND idempotency_key=?", (server_id, consumer, key)))
        if previous:
            require(previous.request_hash == digest, "IDEMPOTENCY_CONFLICT", "MCP idempotency key was used with different arguments")
            return self._replay(previous)
        config = self._config(server_id)
        service, _, connection = self.capacity.define(config, consumer)
        quantities = (QuantityRequirement(key=service), QuantityRequirement(key=connection))
        call_id = new_id("mcp_call")
        dispatch_state = {"started": False}
        try:
            async with self.capacity.borrow(server_id, consumer) as client:
                async with self.rt.resources.phase(call_id, quantities, timeout=config.timeout_seconds):
                    return await self._invoke(client, call_id, quantities, server_id, tool_name, arguments, consumer, key, run_id, dispatch_state)
        except RuntimeFault as exc:
            if not dispatch_state["started"]:
                exc.details["dispatched"] = False
            raise

    def _replay(self, previous):
        if previous.id in self.result_cache:
            return {**self.result_cache[previous.id][0], "reused": True}
        if previous.result_json:
            return {**json.loads(previous.result_json), "reused": True}
        code = ("MCP_RESULT_NOT_RETAINED" if previous.state in {"SUCCEEDED", "FAILED"} else
                "OUTCOME_UNCERTAIN" if previous.state == "UNCERTAIN" else "MCP_CALL_IN_PROGRESS")
        raise RuntimeFault(code, "This MCP invocation was already accepted; it will not be repeated automatically",
                           details={"call_id": previous.id, "state": previous.state})

    async def _invoke(self, client, call_id, quantities, server_id, tool_name, arguments, consumer, key, run_id, dispatch_state):
        config = self._config(server_id)
        require(config == client.config and config.enabled, "MCP_CONFIG_CHANGED", "MCP configuration changed before dispatch")
        if run_id:
            await self.authorize_run(run_id, server_id, tool_name)
        if not self.catalogs.get(server_id, {}).get("discovered_at"):
            await self._discover(server_id, client)
        elif not hasattr(client, "catalog"):
            await self._discover(server_id, client, publish=False)
        tool = next(
            (
                t
                for t in self.catalogs[server_id].get("tools", [])
                if t["name"] == tool_name
            ),
            None,
        )
        require(
            tool is not None,
            "NOT_FOUND",
            "Tool is not in this MCP server's discovered catalog",
        )
        actual = next((item for item in client.catalog.get("tools", []) if item["name"] == tool_name), None)
        require(actual and content_hash(actual) == content_hash(tool), "MCP_SCHEMA_CHANGED",
                "This connection's tool schema differs from the discovered snapshot; refresh the catalog before a new call",
                details={"dispatched": False})
        require(client.info.get("protocolVersion") == self.catalogs[server_id].get("protocolVersion"),
                "MCP_PROTOCOL_CHANGED", "Actual MCP handshake differs from the discovered protocol version",
                details={"dispatched": False})
        require(
            config.allowed_tools is None or tool_name in config.allowed_tools,
            "PERMISSION_DENIED",
            "Tool is disabled in this MCP server's allowlist",
        )
        try:
            jsonschema.validate(
                arguments, tool.get("inputSchema") or {"type": "object"}
            )
        except jsonschema.ValidationError as exc:
            raise RuntimeFault(
                "INVALID_REQUEST",
                "MCP arguments do not match the tool schema",
                details={"field": ".".join(map(str, exc.absolute_path))},
            ) from None
        digest = content_hash({"tool": tool_name, "arguments": arguments})
        await self.capacity.initialize(client, run_id)

        def claim(db):
            previous = one(
                db,
                "SELECT * FROM mcp_calls WHERE server_id=? AND consumer_id=? AND idempotency_key=?",
                (server_id, consumer, key),
            )
            if previous:
                require(
                    previous.request_hash == digest,
                    "IDEMPOTENCY_CONFLICT",
                    "MCP idempotency key was used with different arguments",
                )
                return previous
            db.execute(
                "INSERT INTO mcp_calls(id,server_id,consumer_id,tool_name,state,idempotency_key,request_hash,run_id,started_at) VALUES(?,?,?,?,'DISPATCHED',?,?,?,?)",
                (
                    call_id,
                    server_id,
                    consumer,
                    tool_name,
                    key,
                    digest,
                    run_id,
                    now_ms(),
                ),
            )
            db.execute("INSERT INTO mcp_call_resources(call_id,connection_owner,quantities_json,state) VALUES(?,?,?,'HELD')",
                       (call_id, client.resource_owner, json_dump(quantities)))
            return None

        previous = await self.rt.store.call(claim, transaction=True)
        dispatch_state["started"] = True
        if previous:
            return self._replay(previous)
        reply_received = False
        try:
            result = await client.request(
                "tools/call", {"name": tool_name, "arguments": arguments}, operation_id=call_id
            )
            reply_received = True
            return await self.complete_call(call_id, result, config, tool_name)
        except BaseException as exc:
            readonly = tool.get("annotations", {}).get("readOnlyHint") is True
            if isinstance(exc, RuntimeFault):
                exc.details["call_id"] = call_id
            not_dispatched = ((isinstance(exc, RuntimeFault) and exc.details.get("dispatched") is False)
                              or (not reply_received and call_id not in client.operations))
            state = "FAILED" if readonly or not_dispatched else "UNCERTAIN"
            error = (
                exc.as_dict()
                if isinstance(exc, RuntimeFault)
                else {
                    "code": "MCP_CANCELLED"
                    if isinstance(exc, asyncio.CancelledError)
                    else "MCP_REQUEST_FAILED",
                    "safe_message": "MCP invocation ended without a result",
                }
            )
            if not self.rt.store.closed:
                await self.rt.store.call(
                    lambda db: db.execute(
                        "UPDATE mcp_calls SET state=?,error_json=?,ended_at=? WHERE id=?",
                        (state, json_dump(error), now_ms(), call_id),
                    ),
                    transaction=True,
                )
                if not reply_received and not not_dispatched:
                    await self.capacity.isolate(call_id, client)
                else:
                    await self.rt.store.call(lambda db: db.execute("UPDATE mcp_call_resources SET state='RELEASED' WHERE call_id=?", (call_id,)), transaction=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not readonly and not not_dispatched:
                raise RuntimeFault(
                    "OUTCOME_UNCERTAIN",
                    "MCP tool outcome is uncertain; verify the upstream action before retrying",
                    details={"call_id": call_id, "cause": error["code"]},
                ) from None
            raise

    async def calls(self, server_id=None, limit=50):
        require(
            isinstance(limit, int) and 1 <= limit <= 200,
            "INVALID_REQUEST",
            "Call history limit must be between 1 and 200",
        )
        if server_id:
            self._config(server_id)
        query = "SELECT id,server_id,consumer_id,tool_name,state,error_json,run_id,started_at,ended_at FROM mcp_calls"
        query += " WHERE server_id=?" if server_id else ""
        query += " ORDER BY started_at DESC,rowid DESC LIMIT ?"
        records = await self.rt.store.call(
            lambda db: rows(db, query, (server_id, limit) if server_id else (limit,))
        )
        return [
            {
                **{k: v for k, v in row.items() if k != "error_json"},
                "error": json.loads(row.error_json) if row.error_json else None,
                "duration_ms": row.ended_at - row.started_at if row.ended_at else None,
            }
            for row in records
        ]

    async def get_call(self, call_id=None, *, server_id=None, consumer_id=None, idempotency_key=None):
        by_id = isinstance(call_id, str) and bool(call_id)
        require(by_id != any(value is not None for value in (server_id, consumer_id, idempotency_key)),
                "INVALID_REQUEST", "Choose call_id or server_id, consumer_id and idempotency_key")
        if by_id:
            query, params = "c.id=?", (call_id,)
        else:
            self._config(server_id)
            require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256,
                    "INVALID_REQUEST", "Supply the original bounded idempotency key")
            query, params = "c.server_id=? AND c.consumer_id=? AND c.idempotency_key=?", (server_id, self._consumer(consumer_id), idempotency_key)
        row = await self.rt.store.call(lambda db: one(db,
            "SELECT c.*,r.connection_owner,r.state AS resource_state,r.evidence FROM mcp_calls c "
            "LEFT JOIN mcp_call_resources r ON r.call_id=c.id WHERE " + query, params))
        require(row is not None, "NOT_FOUND", "MCP call receipt does not exist")
        cached = self.result_cache.get(row.id)
        result = json.loads(row.result_json) if row.result_json else cached[0] if cached else None
        return {"call_id": row.id, "server_id": row.server_id, "consumer_id": row.consumer_id,
                "tool_name": row.tool_name, "state": row.state,
                "error": json.loads(row.error_json) if row.error_json else None,
                "resource_state": row.resource_state, "connection_owner": row.connection_owner,
                "started_at": row.started_at, "ended_at": row.ended_at,
                "result_retained": result is not None, "result": result,
                "evidence": row.evidence}

    @staticmethod
    def tool_error(result):
        if not result.get("isError"):
            return None
        value = result.get("structuredContent")
        text = next((block.get("text", "")[:4096] for block in result.get("content", [])
                     if isinstance(block, dict) and block.get("type") == "text"), "")
        if not isinstance(value, dict) and text:
            try:
                value = json.loads(text)
            except ValueError:
                value = None
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            value = value["error"]
        value = value if isinstance(value, dict) else {}
        message = value.get("safe_message") or value.get("message") or value.get("error") or text
        details = {"stage": "tool"}
        for key in ("code", "path", "issues"):
            if key in value:
                item = value[key]
                details["upstream_code" if key == "code" else key] = item if len(json_dump(item).encode("utf-8")) <= 2048 else str(item)[:2048]
        return {"code": "MCP_TOOL_ERROR", "category": "mcp", "retryable": False,
                "safe_message": str(message or "Upstream MCP tool reported an error")[:1024], "details": details}

    async def complete_call(self, call_id, value, config, tool_name, *, connection_owner=None):
        """Commit an actual terminal response, including one observed after the caller left."""
        value = self.rt.vault.redact(value)
        error = self.tool_error(value)
        result = self.bounded_result({**value, "call_id": call_id, "server_id": config.id,
            "tool_name": tool_name, "untrusted_tool_result": True, "reused": False}, config.max_output_bytes)
        def complete(db):
            if connection_owner is not None:
                held = one(db, "SELECT connection_owner FROM mcp_call_resources WHERE call_id=?", (call_id,))
                if held is None or held.connection_owner != connection_owner:
                    return False
            db.execute("UPDATE mcp_calls SET state=?,result_json=?,error_json=?,ended_at=? WHERE id=?",
                       ("FAILED" if error else "SUCCEEDED", json_dump(result) if config.persist_results else None,
                        json_dump(error) if error else None, now_ms(), call_id))
            db.execute("UPDATE mcp_call_resources SET state='RELEASED' WHERE call_id=?", (call_id,))
            return True
        if not await self.rt.store.call(complete, transaction=True):
            return None
        size = len(json_dump(result).encode())
        previous = self.result_cache.pop(call_id, None)
        self.result_cache_bytes += size - (previous[1] if previous else 0)
        self.result_cache[call_id] = (result, size)
        while len(self.result_cache) > 64 or self.result_cache_bytes > 16777216:
            _, (_, removed) = self.result_cache.popitem(last=False)
            self.result_cache_bytes -= removed
        return result

    async def _external_request(self, server_id, consumer_id, method, params):
        config = self._config(server_id)
        require(
            config.expose_to_codex,
            "PERMISSION_DENIED",
            "This MCP server is not exposed to external clients",
        )
        consumer = self._consumer(consumer_id)
        service, _, connection = self.capacity.define(config, consumer)
        quantities = (QuantityRequirement(key=service), QuantityRequirement(key=connection))
        call_id = new_id("mcp_read")
        async with self.capacity.borrow(server_id, consumer) as client:
            async with self.rt.resources.phase(call_id, quantities, timeout=config.timeout_seconds):
                current = self._config(server_id)
                require(current == config and current.expose_to_codex and current.enabled,
                        "MCP_CONFIG_CHANGED", "MCP configuration changed before dispatch")
                if self.capacity.policy(server_id).initialization and not client.initialized:
                    await self._discover(server_id, client, publish=not self.catalogs.get(server_id, {}).get("discovered_at"))
                    await self.capacity.initialize(client, None)
                def intent(db):
                    db.execute("INSERT INTO mcp_calls(id,server_id,consumer_id,tool_name,idempotency_key,request_hash,state,started_at) VALUES(?,?,?,?,?,?,'DISPATCHED',?)",
                               (call_id, server_id, consumer, method, call_id, content_hash(params), now_ms()))
                    db.execute("INSERT INTO mcp_call_resources(call_id,connection_owner,quantities_json,state) VALUES(?,?,?,'HELD')",
                               (call_id, client.resource_owner, json_dump(quantities)))
                await self.rt.store.call(intent, transaction=True)
                reply_received = False
                try:
                    value = await client.request(method, params, operation_id=call_id)
                    reply_received = True
                    value = self.rt.vault.redact(value)
                    require(len(json_dump(value).encode()) <= config.max_output_bytes,
                            "MCP_OUTPUT_LIMIT", "MCP response exceeds its output limit")
                except BaseException as exc:
                    not_dispatched = ((isinstance(exc, RuntimeFault) and exc.details.get("dispatched") is False)
                                      or (not reply_received and call_id not in client.operations))
                    error = exc.as_dict() if isinstance(exc, RuntimeFault) else {
                        "code": "MCP_CANCELLED" if isinstance(exc, asyncio.CancelledError) else "MCP_REQUEST_FAILED",
                        "safe_message": "MCP read ended without a response"}
                    def failed(db):
                        db.execute("UPDATE mcp_calls SET state='FAILED',error_json=?,ended_at=? WHERE id=?", (json_dump(error), now_ms(), call_id))
                        if reply_received or not_dispatched:
                            db.execute("UPDATE mcp_call_resources SET state='RELEASED' WHERE call_id=?", (call_id,))
                    await self.rt.store.call(failed, transaction=True)
                    if not reply_received and not not_dispatched:
                        await self.capacity.isolate(call_id, client)
                    if isinstance(exc, RuntimeFault):
                        exc.details["call_id"] = call_id
                    raise
                def completed(db):
                    db.execute("UPDATE mcp_calls SET state='SUCCEEDED',ended_at=? WHERE id=?", (now_ms(), call_id))
                    db.execute("UPDATE mcp_call_resources SET state='RELEASED' WHERE call_id=?", (call_id,))
                await self.rt.store.call(completed, transaction=True)
                return {**value, "untrusted_tool_result": True}

    async def resources(self, server_id, consumer_id="codex", cursor=None):
        return await self._external_request(
            server_id, consumer_id, "resources/list", {"cursor": cursor}
        )

    async def read_resource(self, server_id, uri, consumer_id="codex"):
        require(
            isinstance(uri, str) and 0 < len(uri) <= 4096,
            "INVALID_REQUEST",
            "A bounded resource URI is required",
        )
        return await self._external_request(
            server_id, consumer_id, "resources/read", {"uri": uri}
        )

    async def prompts(self, server_id, consumer_id="codex", cursor=None):
        return await self._external_request(
            server_id, consumer_id, "prompts/list", {"cursor": cursor}
        )

    async def get_prompt(self, server_id, name, arguments=None, consumer_id="codex"):
        require(
            isinstance(name, str)
            and len(name) <= 256
            and (arguments is None or isinstance(arguments, dict)),
            "INVALID_REQUEST",
            "Invalid MCP prompt request",
        )
        return await self._external_request(
            server_id,
            consumer_id,
            "prompts/get",
            {"name": name, "arguments": arguments},
        )

    def _codex_entries(self):
        codex_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        path = codex_root / "config.toml"
        if not path.is_file():
            return {}
        require(
            path.stat().st_size <= 2097152,
            "INVALID_REQUEST",
            "Codex configuration is too large to import",
        )
        try:
            return tomllib.loads(path.read_text(encoding="utf-8")).get(
                "mcp_servers", {}
            )
        except (OSError, ValueError):
            raise RuntimeFault(
                "MCP_IMPORT_FAILED", "Cannot read the local Codex MCP configuration"
            ) from None

    async def codex_candidates(self):
        result = []
        for name, value in self._codex_entries().items():
            if not isinstance(value, dict):
                continue
            result.append(
                {
                    "name": name,
                    "transport": "stdio" if value.get("command") else "streamable-http",
                    "enabled": value.get("enabled", True),
                    "has_credentials": bool(
                        value.get("env")
                        or value.get("env_vars")
                        or value.get("http_headers")
                        or value.get("env_http_headers")
                        or value.get("bearer_token_env_var")
                    ),
                    "registered": self._import_id(name) in self.configs,
                }
            )
        return result

    @staticmethod
    def _import_id(name):
        name = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")[:56]
        return name if name and name[0].isalpha() else "mcp-" + name

    async def import_codex(self, names):
        require(
            isinstance(names, list)
            and 0 < len(names) <= 32
            and all(isinstance(n, str) for n in names),
            "INVALID_REQUEST",
            "Choose between 1 and 32 exact Codex MCP registration names",
        )
        entries = self._codex_entries()
        imported, skipped, errors = [], [], []
        for name in dict.fromkeys(names):
            try:
                require(
                    name in entries, "NOT_FOUND", "Codex MCP registration was not found"
                )
                source = entries[name]
                identity = self._import_id(name)
                if identity in self.configs:
                    skipped.append({"name": name, "id": identity})
                    continue
                env = dict(source.get("env", {}))
                for entry in source.get("env_vars", []):
                    require(
                        isinstance(entry, str)
                        or entry.get("source", "local") == "local",
                        "MCP_IMPORT_UNSUPPORTED",
                        "Remote executor environment variables cannot be imported locally",
                    )
                    key = entry if isinstance(entry, str) else entry["name"]
                    require(
                        key in os.environ,
                        "MCP_CREDENTIAL_REQUIRED",
                        "A Codex environment variable is not available to this platform process",
                    )
                    env[key] = os.environ[key]
                headers = dict(source.get("http_headers", {}))
                for key, variable in source.get("env_http_headers", {}).items():
                    require(
                        variable in os.environ,
                        "MCP_CREDENTIAL_REQUIRED",
                        "An HTTP header environment variable is not available",
                    )
                    headers[key] = os.environ[variable]
                if source.get("bearer_token_env_var"):
                    require(
                        source["bearer_token_env_var"] in os.environ,
                        "MCP_CREDENTIAL_REQUIRED",
                        "The bearer token environment variable is unavailable",
                    )
                    headers["Authorization"] = (
                        "Bearer " + os.environ[source["bearer_token_env_var"]]
                    )
                require(
                    not source.get("http_headers_helper") and not source.get("oauth"),
                    "MCP_IMPORT_UNSUPPORTED",
                    "Configure an explicit platform header for OAuth or dynamic-header MCP servers",
                )
                require(
                    not {"ma_runtime.cli", "oea", "oea.cli"}.intersection(source.get("args", []))
                    and Path(source.get("command", "")).stem.lower() not in {"oea", "multi-agent"},
                    "MCP_IMPORT_UNSUPPORTED",
                    "Importing the platform's own bridge would create a loop",
                )
                config = MCPServerConfig(
                    id=identity,
                    name=name,
                    description="Imported from local Codex MCP configuration",
                    command=source.get("command", ""),
                    args=tuple(source.get("args", [])),
                    cwd=source.get("cwd", ""),
                    url=source.get("url", ""),
                    transport="stdio" if source.get("command") else "streamable-http",
                    enabled=source.get("enabled", True),
                    allowed_tools=tuple(source["enabled_tools"])
                    if "enabled_tools" in source
                    else None,
                    startup_timeout_seconds=source.get("startup_timeout_sec", 30),
                    timeout_seconds=source.get("tool_timeout_sec", 60),
                )
                if source.get("disabled_tools"):
                    require(
                        config.allowed_tools is not None,
                        "MCP_IMPORT_UNSUPPORTED",
                        "Import a positive tool allowlist instead of a denylist-only configuration",
                    )
                    config.allowed_tools = tuple(
                        t
                        for t in config.allowed_tools
                        if t not in source["disabled_tools"]
                    )
                imported.append(
                    await self.configure(config, {"env": env, "headers": headers})
                )
            except (RuntimeFault, ValueError, TypeError, KeyError) as exc:
                fault = (
                    exc
                    if isinstance(exc, RuntimeFault)
                    else RuntimeFault(
                        "MCP_IMPORT_FAILED",
                        "Codex registration contains unsupported configuration",
                    )
                )
                errors.append({"name": name, "error": fault.as_dict()})
        return {"imported": imported, "skipped": skipped, "errors": errors}

    async def codex_config(self):
        return {
            "mcp_servers": {
                "oea": {
                    "command": sys.executable,
                    "args": [
                        "-B",
                        "-m",
                        "oea",
                        "mcp",
                        "--connect",
                        "--compact",
                        "--data-dir",
                        str(self.rt.root),
                    ],
                    "env": {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                    "startup_timeout_sec": 30,
                    "tool_timeout_sec": 180,
                    "enabled_tools": ["platform_guide", "platform_call"],
                }
            }
        }

    async def aclose(self):
        self.closing = True
        await asyncio.gather(
            *(client.aclose(force=True) for client in self.connections.values()),
            return_exceptions=True,
        )
        for client in self.connections.values():
            if client.closed:
                await self.capacity.connection_closed(client)
        await self.capacity.drain()
        self.connections.clear()
