"""MCP connection state and physical-call capacity use the Runtime resource kernel."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

from .errors import require
from .models import MCPResourcePolicy, QuantityRequirement, content_hash, json_dump
from .store import emit, one, rows


class MCPResources:
    def __init__(self, registry):
        self.registry = registry
        self.rt = registry.rt
        self.policies = {}
        self.uncertain_connections = set()

    def policy(self, server_id):
        return self.policies.get(server_id) or MCPResourcePolicy(
            max_service_calls=self.registry._config(server_id).max_concurrency)

    def keys(self, config, consumer):
        effective = "shared" if config.session_scope == "shared" else consumer
        return ("mcp:service:" + config.id, "mcp:connections:" + config.id,
                "mcp:connection:" + config.id + ":" + content_hash(effective)[:24])

    def define(self, config, consumer):
        policy = self.policy(config.id)
        service, connections, connection = self.keys(config, consumer)
        resources = self.rt.resources
        resources.define(service, policy.max_service_calls, unit="call")
        resources.define(connections, config.max_connections, unit="connection", reserved=policy.reserved_connections)
        resources.define(connection, config.max_concurrency, unit="call")
        return service, connections, connection

    async def open(self):
        policies, held = await self.rt.store.call(lambda db: (
            rows(db, "SELECT * FROM mcp_resource_policies"),
            rows(db, "SELECT r.*,c.server_id,c.consumer_id FROM mcp_call_resources r JOIN mcp_calls c ON c.id=r.call_id WHERE r.state!='RELEASED'")))
        self.policies = {row.server_id: MCPResourcePolicy.model_validate_json(row.policy_json) for row in policies}
        for row in held:
            config = self.registry._config(row.server_id)
            _, connections, _ = self.define(config, row.consumer_id)
            self.rt.resources.restore(row.connection_owner, (), (QuantityRequirement(key=connections),))
            self.rt.resources.isolate(row.connection_owner, "MCP operation requires stop confirmation")
            self.uncertain_connections.add(row.connection_owner)
            quantities = tuple(QuantityRequirement.model_validate(item) for item in json.loads(row.quantities_json))
            self.rt.resources.restore(row.call_id, (), quantities)
            self.rt.resources.isolate(row.call_id, "MCP operation requires stop confirmation")
        if held:
            await self.rt.store.call(lambda db: db.execute("UPDATE mcp_call_resources SET state='ISOLATED' WHERE state='HELD'"), transaction=True)

    async def configure(self, server_id, policy):
        config = self.registry._config(server_id)
        policy = MCPResourcePolicy.model_validate(policy)
        require(policy.reserved_connections < config.max_connections, "INVALID_REQUEST", "Reserved connections must be below the connection limit")
        async with self.registry.lock:
            clients = [client for (sid, _), client in self.registry.connections.items() if sid == server_id]
            require(not any(client.active or client.pins or client.resource_owner in self.uncertain_connections for client in clients),
                    "RESOURCE_BUSY", "Wait for active and unresolved MCP calls before changing connection policy")
            require(not any(owner.startswith("mcp:connection:" + server_id + ":") for owner in self.uncertain_connections),
                    "RESOURCE_BUSY", "Reconcile unresolved MCP capacity before changing its connection policy")
            for item in policy.initialization:
                self.initialization_tool(server_id, item.tool_name)
            for client in clients:
                await client.aclose()
                self.rt.resources.release(client.resource_owner)
            self.registry.connections = {key: client for key, client in self.registry.connections.items() if key[0] != server_id}
            await self.rt.store.call(lambda db: db.execute(
                "INSERT INTO mcp_resource_policies(server_id,policy_json) VALUES(?,?) ON CONFLICT(server_id) DO UPDATE SET policy_json=excluded.policy_json",
                (server_id, json_dump(policy))), transaction=True)
            self.policies[server_id] = policy
            self.define(config, "console")
        self.rt.resources.changed.set()
        return await self.inspect(server_id)

    async def inspect(self, server_id):
        self.registry._config(server_id)
        return {"server_id": server_id, "policy": self.policy(server_id).model_dump(mode="json"),
                "connections": [{"consumer_id": consumer, "pins": client.pins, "active": client.active,
                                 "isolated": client.resource_owner in self.uncertain_connections,
                                 "handshake": client.info, "schema_fingerprint": getattr(client, "schema_fingerprint", None)}
                                for (sid, consumer), client in self.registry.connections.items() if sid == server_id]}

    def initialization_tool(self, server_id, name):
        config = self.registry._config(server_id)
        tool = next((item for item in self.registry.catalogs.get(server_id, {}).get("tools", []) if item["name"] == name), None)
        require(tool is not None, "MCP_INITIALIZATION_INVALID", "Initialization must select a discovered connection-state tool")
        require(config.allowed_tools is None or name in config.allowed_tools, "PERMISSION_DENIED", "Initialization tool is not granted")
        return tool

    async def initialize(self, client, run_id):
        policy = self.policy(client.config.id)
        if client.initialized:
            return
        async with client.initialization_lock:
            if client.initialized:
                return
            for item in policy.initialization:
                self.initialization_tool(client.config.id, item.tool_name)
                expected = self.initialization_tool(client.config.id, item.tool_name)
                actual = next((tool for tool in client.catalog.get("tools", []) if tool["name"] == item.tool_name), None)
                require(actual and content_hash(actual) == content_hash(expected), "MCP_SCHEMA_CHANGED", "Initialization tool changed on this connection")
                if run_id:
                    await self.registry.authorize_run(run_id, client.config.id, item.tool_name)
                result = await client.request("tools/call", {"name": item.tool_name, "arguments": item.arguments})
                require(not result.get("isError"), "MCP_INITIALIZATION_FAILED", "Connection state initialization failed")
            client.initialized = True

    async def reclaim(self, server_id, *, pressure=False):
        # Called under the registry lock. Closed or unknown operations never
        # disappear merely because their transport has been disconnected.
        policy = self.policy(server_id)
        now = time.monotonic()
        candidates = sorted(((key, client) for key, client in self.registry.connections.items() if key[0] == server_id),
                            key=lambda pair: pair[1].last_used)
        for key, client in candidates:
            if client.active or client.pins or client.children or client.resource_owner in self.uncertain_connections:
                continue
            if client.closed or (policy.state_mode != "persistent" and (pressure or now - client.last_used >= policy.idle_seconds)):
                await client.aclose()
                self.registry.connections.pop(key, None)
                self.rt.resources.release(client.resource_owner)
                if pressure:
                    break

    @asynccontextmanager
    async def borrow(self, server_id, consumer):
        client = await self.registry._connection(server_id, consumer)
        client.pins += 1
        try:
            yield client
        finally:
            client.pins -= 1
            client.last_used = time.monotonic()
            self.rt.resources.changed.set()

    async def isolate(self, call_id, client):
        self.rt.resources.isolate(call_id, "MCP call did not confirm it stopped")
        self.rt.resources.isolate(client.resource_owner, "MCP call did not confirm it stopped")
        self.uncertain_connections.add(client.resource_owner)
        await self.rt.store.call(lambda db: db.execute("UPDATE mcp_call_resources SET state='ISOLATED' WHERE call_id=?", (call_id,)), transaction=True)

    async def reconcile(self, call_id, *, confirmed_stopped, evidence):
        require(confirmed_stopped and isinstance(evidence, str) and 0 < len(evidence.strip()) <= 4096,
                "RECONCILIATION_REQUIRED", "Provide actual evidence that this MCP operation stopped")
        def commit(db):
            row = one(db, "SELECT * FROM mcp_call_resources WHERE call_id=?", (call_id,))
            require(row is not None, "NOT_FOUND", "MCP call has no resource receipt")
            require(row.state in {"ISOLATED", "RELEASED"}, "RESOURCE_BUSY", "A running MCP call cannot release its own capacity early")
            db.execute("UPDATE mcp_call_resources SET state='RELEASED',evidence=? WHERE call_id=?", (evidence, call_id))
            remaining = one(db, "SELECT call_id FROM mcp_call_resources WHERE connection_owner=? AND state!='RELEASED' LIMIT 1", (row.connection_owner,))
            emit(db, "mcp.capacity_reconciled", payload={"call_id": call_id, "confirmed_stopped": True})
            return row.connection_owner, remaining is None
        owner, last = await self.rt.store.call(commit, transaction=True)
        self.rt.resources.resolve(call_id)
        if last:
            self.uncertain_connections.discard(owner)
            # A live connection still counts against its connection limit.
            self.rt.resources.isolated.pop(owner, None)
            if not any(client.resource_owner == owner and not client.closed for client in self.registry.connections.values()):
                self.rt.resources.release(owner)
        self.rt.resources.changed.set()
        return {"call_id": call_id, "resource_state": "RELEASED", "outcome_changed": False}
