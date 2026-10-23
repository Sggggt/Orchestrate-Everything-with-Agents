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
        self.recoveries = {}
        self.watching = {}
        self.closures = {}
        self.recovery_slots = self.rt.resources.recovery_slots

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
            try:
                evidence = json.loads(row.evidence or "{}")
            except ValueError:
                evidence = {}  # Legacy administrator evidence is plain text.
            closed = isinstance(evidence, dict) and evidence.get("kind") == "connection_closed" and evidence.get("generation") == row.connection_owner
            if not closed:
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
            require(server_id not in self.registry.reconfiguring, "RESOURCE_BUSY", "MCP connection policy is already changing")
            self.registry.reconfiguring.add(server_id)
        try:
            await self.registry.disconnect(server_id)
            await self.rt.store.call(lambda db: db.execute(
                "INSERT INTO mcp_resource_policies(server_id,policy_json) VALUES(?,?) ON CONFLICT(server_id) DO UPDATE SET policy_json=excluded.policy_json",
                (server_id, json_dump(policy))), transaction=True)
            self.policies[server_id] = policy
            self.define(config, "console")
        finally:
            self.registry.reconfiguring.discard(server_id)
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
        # Pin checks and retirement fencing are atomic; transport I/O never holds the registry lock.
        policy = self.policy(server_id)
        now = time.monotonic()
        async with self.registry.lock:
            candidates = [(key, client) for key, client in self.registry.connections.items()
                          if key[0] == server_id and not (client.active or client.pins or client.children or client.closing and not client.closed)
                          and client.resource_owner not in self.uncertain_connections
                          and (client.closed or policy.state_mode != "persistent" and (pressure or now - client.last_used >= policy.idle_seconds))]
            if pressure and candidates:
                candidates = [min(candidates, key=lambda pair: pair[1].last_used)]
            tasks = []
            for key, client in candidates:
                client.closing = True
                task = asyncio.create_task(self.close_idle(key, client), name="mcp-idle-close:" + client.resource_owner)
                self.closures[client.resource_owner] = task
                def finished(done, owner=client.resource_owner):
                    self.closures.pop(owner, None)
                    if not done.cancelled() and done.exception() is not None:
                        asyncio.get_running_loop().call_exception_handler({
                            "message": "Idle MCP transport did not close; capacity remains held", "exception": done.exception()})
                task.add_done_callback(finished)
                tasks.append(task)
        if tasks:
            completion = asyncio.gather(*tasks)
            completion.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            await asyncio.shield(completion)

    async def close_idle(self, key, client):
        try:
            async with self.recovery_slots:
                await client.aclose()
        finally:
            if client.closed:
                async with self.registry.lock:
                    if self.registry.connections.get(key) is client:
                        self.registry.connections.pop(key)
                await self.connection_closed(client)

    async def connection_closed(self, client):
        require(client.closed, "RESOURCE_BUSY", "A live physical connection cannot return its connection slot")
        if client.resource_owner in self.uncertain_connections:
            evidence = json_dump({"kind": "connection_closed", "generation": client.resource_owner})
            await self.rt.store.call(lambda db: db.execute(
                "UPDATE mcp_call_resources SET evidence=? WHERE connection_owner=? AND state!='RELEASED' AND COALESCE(evidence,'')!=?",
                (evidence, client.resource_owner, evidence)), transaction=True)
        self.rt.resources.resolve(client.resource_owner)
        self.rt.resources.retire_connection_capacity(client.resource_owner.partition(":generation_")[0])

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
        request = client.operations.get(call_id)
        def isolate(db):
            db.execute("UPDATE mcp_call_resources SET state='ISOLATED' WHERE call_id=?", (call_id,))
            self.event(db, "mcp.capacity_isolated", call_id, "ISOLATED", "RECOVERING" if request else "NEEDS_EVIDENCE")
        await self.rt.store.call(isolate, transaction=True)
        if request is not None:
            self.watching[call_id] = client
            request.future.add_done_callback(lambda _: self.schedule_recovery(call_id, client))

    @staticmethod
    def event(db, name, call_id, resource_state, recovery_state):
        call = one(db, "SELECT c.state,c.tool_name,c.run_id,t.id AS task_id,t.session_id,t.thread_id FROM mcp_calls c "
                   "LEFT JOIN runs r ON r.id=c.run_id LEFT JOIN tasks t ON t.id=r.task_id WHERE c.id=?", (call_id,))
        emit(db, name, session_id=call.session_id, thread_id=call.thread_id, task_id=call.task_id, run_id=call.run_id,
             payload={"call_id": call_id, "tool": call.tool_name, "resource_state": resource_state,
                      "recovery_state": recovery_state, "state": call.state,
                      "confirmed_stopped": resource_state == "RELEASED"})

    def recovery_state(self, call_id, state):
        if state == "RELEASED":
            return "RELEASED"
        if state != "ISOLATED":
            return "ACTIVE"
        return "RECOVERING" if call_id in self.watching or call_id in self.recoveries else "NEEDS_EVIDENCE"

    def schedule_recovery(self, call_id, client):
        if call_id in self.recoveries or self.rt.store.closed:
            return
        task = asyncio.create_task(self.observe(call_id, client), name="mcp-recovery:" + call_id)
        self.recoveries[call_id] = task
        def finished(done):
            self.recoveries.pop(call_id, None)
            if not done.cancelled() and done.exception() is not None:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "MCP resource recovery failed; capacity remains isolated", "exception": done.exception()})
        task.add_done_callback(finished)

    async def observe(self, call_id, client):
        async with self.recovery_slots:
            request = client.operations.get(call_id)
            if request is None or not request.future.done():
                return False
            client.operations.pop(call_id, None)
            self.watching.pop(call_id, None)
            row = await self.rt.store.call(lambda db: one(db,
                "SELECT c.tool_name,r.connection_owner,r.state FROM mcp_calls c JOIN mcp_call_resources r ON r.call_id=c.id WHERE c.id=?",
                (call_id,)))
            if row is None or row.connection_owner != client.resource_owner:
                return False
            if not request.replied or request.future.cancelled() or request.future.exception() is not None:
                await self.rt.store.call(lambda db: self.event(db, "mcp.capacity_observed", call_id,
                                          row.state, "RELEASED" if row.state == "RELEASED" else "NEEDS_EVIDENCE"), transaction=True)
                return False
            result = await self.registry.complete_call(call_id, request.future.result(), client.config, row.tool_name,
                                                       connection_owner=client.resource_owner)
            if result is None:
                return False
            await self.reconcile(call_id, confirmed_stopped=True, evidence=json_dump({
                "kind": "terminal_mcp_response", "connection_owner": client.resource_owner, "call_id": call_id}))
            await self.rt.tools.observe_mcp_response(call_id, result)
            self.rt.notify()
            return True

    async def drain(self):
        await asyncio.sleep(0)
        tasks = [*self.recoveries.values(), *self.closures.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recover(self, call_id):
        row = await self.rt.store.call(lambda db: one(db,
            "SELECT state,connection_owner FROM mcp_call_resources WHERE call_id=?", (call_id,)))
        require(row is not None, "NOT_FOUND", "MCP call has no resource receipt")
        if row.state == "RELEASED":
            return {"state": "RELEASED", "generation": row.connection_owner}
        client = next((item for item in self.registry.connections.values() if item.resource_owner == row.connection_owner), None)
        request = client.operations.get(call_id) if client is not None else None
        if request is not None and request.future.done():
            task = self.recoveries.get(call_id)
            if task is not None:
                await asyncio.shield(task)
            else:
                await self.observe(call_id, client)
            latest = await self.rt.store.call(lambda db: one(db,
                "SELECT state FROM mcp_call_resources WHERE call_id=?", (call_id,)))
            if latest.state == "RELEASED":
                return {"state": "RELEASED", "generation": row.connection_owner}
        if request is not None and not request.future.done():
            return {"state": "RECOVERING", "generation": row.connection_owner,
                    "reason": "Awaiting the original request's bounded terminal response"}
        return {"state": "NEEDS_EVIDENCE", "generation": row.connection_owner,
                "reason": "No terminal response or upstream stop proof is available; capacity remains held"}

    async def reconcile(self, call_id, *, confirmed_stopped, evidence):
        require(confirmed_stopped and isinstance(evidence, str) and 0 < len(evidence.strip()) <= 4096,
                "RECONCILIATION_REQUIRED", "Provide actual evidence that this MCP operation stopped")
        def commit(db):
            row = one(db, "SELECT * FROM mcp_call_resources WHERE call_id=?", (call_id,))
            require(row is not None, "NOT_FOUND", "MCP call has no resource receipt")
            require(row.state in {"ISOLATED", "RELEASED"}, "RESOURCE_BUSY", "A running MCP call cannot release its own capacity early")
            db.execute("UPDATE mcp_call_resources SET state='RELEASED',evidence=? WHERE call_id=?", (evidence, call_id))
            remaining = one(db, "SELECT call_id FROM mcp_call_resources WHERE connection_owner=? AND state!='RELEASED' LIMIT 1", (row.connection_owner,))
            self.event(db, "mcp.capacity_reconciled", call_id, "RELEASED", "RELEASED")
            return row.connection_owner, remaining is None
        owner, last = await self.rt.store.call(commit, transaction=True)
        self.rt.resources.resolve(call_id)
        if last:
            self.uncertain_connections.discard(owner)
            # A live connection still counts against its connection limit.
            self.rt.resources.isolated.pop(owner, None)
            if not any(client.resource_owner == owner and not client.closed for client in self.registry.connections.values()):
                self.rt.resources.release(owner)
                self.rt.resources.retire_connection_capacity(owner.partition(":generation_")[0])
        self.rt.resources.changed.set()
        return {"call_id": call_id, "resource_state": "RELEASED", "outcome_changed": False}
