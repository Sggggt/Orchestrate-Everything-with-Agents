"""Business MCP connections, versioned discovery, and Run-scoped tool grants."""

import asyncio
import hashlib
import json
import os
import re
import tomllib
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from jsonschema.exceptions import SchemaError

from oea.contracts.configuration import Configuration, Model
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows
from oea.tools.contracts import ToolSpec
from oea.interfaces.registry import operation_context
from oea.tools.gateway import PreparedTool


class MCPServer(Configuration):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=640)
    project_id: str | None = None
    source_directory: Path | None = None
    enabled: bool = True
    removed: bool = False
    expose_to_codex: bool = True
    session_scope: Literal["consumer", "shared"] = "consumer"
    allowed_tools: tuple[str, ...] | None = None
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: tuple[str, ...] = ()
    cwd: Path | None = None
    url: str = ""
    environment: dict[str, str] = Field(default_factory=dict)
    environment_refs: dict[str, str] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120, gt=0, le=3600)

    @model_validator(mode="after")
    def target(self):
        if self.removed and self.enabled:
            raise ValueError("A removed MCP service must be disabled")
        if self.allowed_tools is not None and (len(set(self.allowed_tools)) != len(self.allowed_tools) or any(not name for name in self.allowed_tools)):
            raise ValueError("Allowed MCP tool names must be nonempty and unique")
        if self.transport == "stdio":
            if not self.command or self.url or self.header_refs:
                raise ValueError("A stdio service uses a command and optional environment references")
        elif not self.url or self.command or self.environment or self.environment_refs or self.cwd:
            raise ValueError("An HTTP service uses a URL and optional header references")
        if self.url:
            Model(base_url=self.url)
        for reference in (*self.environment_refs.values(), *self.header_refs.values()):
            Model(credential_ref=reference)
        if set(self.environment) & set(self.environment_refs):
            raise ValueError("An environment entry has one value source")
        return self


def alias(server_id, name, revision):
    digest = hashlib.sha256(encode([server_id, name, revision]).encode("utf-8")).hexdigest()[:20]
    return "mcp_" + server_id[:38] + "_" + digest


class Connection:
    """Own all AnyIO session scopes in one task; calls remain concurrent."""
    def __init__(self, registry, definition, revision):
        self.registry, self.definition, self.revision = registry, definition, revision
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.session = None
        self.task = asyncio.create_task(self.serve(), name="mcp:" + definition.id)

    async def serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        import httpx

        value, host = self.definition, self.registry.host
        try:
            async with AsyncExitStack() as stack:
                if value.transport == "stdio":
                    log_dir = host.root / "mcp-logs"
                    log_dir.mkdir(exist_ok=True)
                    log = stack.enter_context((log_dir / (value.id + ".stderr.log")).open("a", encoding="utf-8"))
                    environment = {**value.environment, **{name: self.registry.secret(ref) for name, ref in value.environment_refs.items()}}
                    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(command=value.command[0], args=list(value.command[1:]),
                        cwd=value.cwd, env=environment, encoding="utf-8"), errlog=log))
                else:
                    headers = {name: self.registry.secret(ref) for name, ref in value.header_refs.items()}
                    for name, secret in headers.items():
                        if name.lower() == "authorization" and " " in secret:
                            host.vault.known_secrets.add(secret.partition(" ")[2])
                    client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(value.timeout_seconds, read=None), follow_redirects=False,
                        trust_env=urlsplit(value.url).hostname not in {"localhost", "127.0.0.1", "::1"},
                        limits=httpx.Limits(max_connections=None, max_keepalive_connections=16)))
                    streams = await stack.enter_async_context(streamable_http_client(value.url, http_client=client))
                self.session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                async with asyncio.timeout(value.timeout_seconds):
                    initialized = await self.session.initialize()
                if initialized.serverInfo.name == "oea":
                    self.ready.set_exception(RuntimeFault("CONTROL_BRIDGE_FORBIDDEN", "Platform control MCP cannot be bound as a business MCP service"))
                    return
                self.ready.set_result(self)
                await self.stop.wait()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            raise

    async def aclose(self):
        self.stop.set()
        await asyncio.gather(self.task, return_exceptions=True)


class UpstreamTool:
    def __init__(self, registry, server_id, revision, value):
        self.registry, self.server_id, self.revision = registry, server_id, revision
        self.upstream_name = value["upstream_name"]
        self.spec = ToolSpec.model_validate(value["spec"])

    def describe(self):
        return self.spec

    async def invoke(self, request):
        # Dispatch checks current grants again after any connection awaits.
        if request["run_id"] is not None:
            owner = await self.registry.host.store.call(lambda db: one(db, "SELECT instance_id FROM runs WHERE id=?", (request["run_id"],)))
            consumer = encode(["instance", owner["instance_id"]])
        else:
            owner = await self.registry.host.store.call(lambda db: one(db, "SELECT project_id,consumer_id FROM tool_intents WHERE id=?", (request["intent_id"],)))
            consumer = self.registry.controller_scope(owner["project_id"], owner["consumer_id"])
        connection = await self.registry.connection(self.server_id, self.revision, consumer=consumer)
        if request["run_id"] is not None:
            await self.registry.host.authorize(request["run_id"], self.spec.name)
        else:
            await self.registry.authorize_intent(request["intent_id"], self.spec.name)
        result = await connection.session.call_tool(self.upstream_name, request["arguments"],
            read_timeout_seconds=timedelta(seconds=self.spec.timeout_seconds), meta={"oea/action_id": request["action_id"], "oea/operation_key": request["operation_key"]})
        return {"isError": result.isError, "content": [item.model_dump(mode="json", exclude_none=True) for item in result.content],
                "structured_content": result.structuredContent}


class UpstreamMCP:
    def __init__(self, host):
        self.host, self.connections, self.configurations = host, {}, {}
        self.retiring, self.retirement, self.retirement_dirty = set(), None, False
        self.discovering, self.calling = {}, {}

    async def open(self):
        values = await self.host.store.call(lambda db: rows(db, "SELECT config_json,revision FROM mcp_servers"))
        self.configurations = {definition.id: (definition, value["revision"]) for value in values for definition in [MCPServer.model_validate_json(value["config_json"])]}

    def secret(self, reference):
        value = self.host.secret(reference)
        require(value, "SECRET_UNAVAILABLE", "Configured MCP credential is unavailable")
        return value

    async def save(self, definition: MCPServer, *, expected_revision: int | None = None):
        self.host.assert_open()
        definition = MCPServer.model_validate(definition)
        if definition.cwd:
            definition = definition.model_copy(update={"cwd": definition.cwd.expanduser().resolve()})
        if definition.source_directory:
            definition = definition.model_copy(update={"source_directory": definition.source_directory.expanduser().resolve()})
        def write(db):
            if definition.project_id:
                require(one(db, "SELECT 1 FROM projects WHERE id=?", (definition.project_id,)), "NOT_FOUND", "Project does not exist")
            previous = one(db, "SELECT * FROM mcp_servers WHERE id=?", (definition.id,))
            require(expected_revision is None or expected_revision == (previous["revision"] if previous else 0), "REVISION_CONFLICT", "MCP configuration changed")
            require(not previous or previous["project_id"] == definition.project_id, "PROJECT_MISMATCH", "MCP service cannot change its project")
            revision = previous["revision"] + 1 if previous else 1
            db.execute("INSERT INTO mcp_servers VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,revision=excluded.revision,updated_at=excluded.updated_at",
                (definition.id, definition.project_id, definition.model_dump_json(), revision, now_ms()))
            event(db, "mcp.saved", project_id=definition.project_id, data={"server_id": definition.id, "revision": revision, "removed": definition.removed})
            return revision
        revision = await self.host.store.call(write, transaction=True)
        self.configurations[definition.id] = (definition, revision)
        self.retiring.add(definition.id)
        await self.retire_idle()
        self.host.notify()
        return {"definition": definition.model_dump(mode="json"), "revision": revision}

    async def retire_idle(self):
        busy = {(provider.server_id, provider.revision) for identity, name in self.host.tools.names.items()
                if isinstance(provider := self.host.tools.providers.get(name), UpstreamTool) and not self.host.tools.inflight[identity].done()}
        for server_id in tuple(self.retiring):
            saved = self.configurations.get(server_id)
            if saved is None:
                self.retiring.discard(server_id)
                continue
            current = saved[1]
            for key, connection in tuple(self.connections.items()):
                if key[0] == server_id and key[1] != current and key[:2] not in busy and connection.ready.done():
                    await connection.aclose()
                    if self.connections.get(key) is connection:
                        self.connections.pop(key)
            if not any(key[0] == server_id and key[1] != current for key in self.connections):
                self.retiring.discard(server_id)

    def after_settle(self):
        """Coalesce actual settlement events; never poll for idle connections."""
        if not self.retiring or self.host.closing:
            return
        self.retirement_dirty = True
        if self.retirement is not None and not self.retirement.done():
            return
        async def drain():
            while self.retirement_dirty:
                self.retirement_dirty = False
                await self.retire_idle()
        task = self.retirement = asyncio.create_task(drain(), name="mcp-retirement")
        def finished(completed):
            if self.retirement is completed:
                self.retirement = None
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)

    async def remove(self, server_id: str, project_id: str, expected_revision: int):
        """Disable and hide a service while retaining its source, secrets, and receipts."""
        saved = await self.get(server_id)
        definition = MCPServer.model_validate(saved["definition"])
        require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "MCP service belongs to another project")
        await self.host.projects.get(project_id)
        if definition.removed:
            require(expected_revision in {saved["revision"], saved["revision"] - 1}, "REVISION_CONFLICT", "MCP configuration changed")
            return saved
        require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "MCP configuration changed")
        return await self.save(definition.model_copy(update={"removed": True, "enabled": False}), expected_revision=expected_revision)

    @staticmethod
    def codex_identity(name):
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            return name
        label = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")[:44]
        return "codex-" + label + "-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def read_codex(config_path):
        source = Path(config_path).expanduser()
        require(source.is_absolute() and not str(source).startswith(("\\\\", "//")), "INVALID_REQUEST", "Choose an explicit local Codex TOML file")
        try:
            require(source.is_file() and source.stat().st_size <= 2097152, "MCP_IMPORT_FAILED", "Codex configuration is unavailable or too large")
            entries = tomllib.loads(source.read_text(encoding="utf-8-sig")).get("mcp_servers", {})
        except (OSError, UnicodeError, ValueError):
            raise RuntimeFault("MCP_IMPORT_FAILED", "Cannot read the selected Codex MCP configuration") from None
        require(isinstance(entries, dict), "MCP_IMPORT_FAILED", "Codex MCP registrations must be a table")
        return entries

    def codex_definition(self, name, source, project_id):
        """Normalize one selected registration; never connect during import."""
        require(isinstance(source, dict), "MCP_IMPORT_UNSUPPORTED", "MCP registration must be a table")
        require(not any(source.get(key) for key in ("http_headers_helper", "oauth", "executor")),
                "MCP_IMPORT_UNSUPPORTED", "Dynamic authentication and remote executors require explicit platform configuration")
        command, args = source.get("command"), source.get("args", [])
        require(isinstance(args, list) and all(isinstance(value, str) for value in args), "MCP_IMPORT_UNSUPPORTED", "MCP command arguments must be text")
        require(command is None or isinstance(command, str), "MCP_IMPORT_UNSUPPORTED", "MCP command must be text")
        require(Path(command or "").stem.lower() not in {"oea", "multi-agent"} and not
                {"oea", "oea.interfaces.cli", "oea.stdio", "ma_runtime.cli"}.intersection(args),
                "CONTROL_BRIDGE_FORBIDDEN", "The platform control bridge cannot be imported as a business service")
        allowed = source.get("enabled_tools")
        denied = source.get("disabled_tools", [])
        require((allowed is None or isinstance(allowed, list) and all(isinstance(value, str) for value in allowed)) and
                isinstance(denied, list) and all(isinstance(value, str) for value in denied), "MCP_IMPORT_UNSUPPORTED", "MCP tool filters must be lists of names")
        require(not denied or allowed is not None, "MCP_IMPORT_UNSUPPORTED", "A denylist-only service requires an explicit tool allowlist before import")
        env, headers = source.get("env", {}), source.get("http_headers", {})
        inherited, header_env = source.get("env_vars", []), source.get("env_http_headers", {})
        require(all(isinstance(value, dict) and all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()) for value in (env, headers, header_env)),
                "MCP_IMPORT_UNSUPPORTED", "Environment and header entries must be text mappings")
        require(isinstance(inherited, list) and all(isinstance(value, str) for value in inherited), "MCP_IMPORT_UNSUPPORTED", "Only local environment variable names can be imported")
        bearer = source.get("bearer_token_env_var")
        require(bearer is None or isinstance(bearer, str), "MCP_IMPORT_UNSUPPORTED", "Bearer token reference must be an environment variable name")
        require(not (command and (headers or header_env or bearer)) and not (not command and (env or inherited)),
                "MCP_IMPORT_UNSUPPORTED", "Choose credentials matching the MCP transport")
        definition = MCPServer(id=self.codex_identity(name), name=name[:80], project_id=project_id,
            description="Imported from local Codex MCP configuration", command=(command, *args) if command else (),
            transport="stdio" if command else "streamable_http", url=source.get("url", ""), cwd=source.get("cwd") or None,
            enabled=source.get("enabled", True), allowed_tools=tuple(value for value in allowed if value not in denied) if allowed is not None else None,
            timeout_seconds=source.get("tool_timeout_sec", 120))
        missing = [key for key in (*inherited, *header_env.values(), *((bearer,) if bearer else ())) if key not in os.environ]
        require(not missing, "MCP_CREDENTIAL_REQUIRED", "A referenced environment variable is unavailable to this platform process")
        environment = {**env, **{key: os.environ[key] for key in inherited}}
        credentials = {**headers, **{key: os.environ[variable] for key, variable in header_env.items()}}
        if bearer:
            credentials["Authorization"] = "Bearer " + os.environ[bearer]
        for key, value in environment.items():
            require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and len(value) <= 16384, "MCP_IMPORT_UNSUPPORTED", "MCP environment entry is invalid")
        for key, value in credentials.items():
            require(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) and value and len(value) <= 16384 and "\r" not in value and "\n" not in value,
                    "MCP_IMPORT_UNSUPPORTED", "MCP header entry is invalid")
        return definition, environment, credentials

    async def codex_candidates(self, project_id: str, config_path: Path, *, cursor: int = 0, limit: int = 25):
        """Preview one explicit local TOML file without returning credentials or connecting."""
        require(type(cursor) is int and cursor >= 0 and type(limit) is int and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded page")
        await self.host.projects.get(project_id)
        entries = list((await asyncio.to_thread(self.read_codex, config_path)).items())
        items = []
        for name, source in entries[cursor:cursor + limit]:
            candidate = {"name": name, "id": self.codex_identity(name), "registered": self.codex_identity(name) in self.configurations}
            try:
                definition, environment, headers = self.codex_definition(name, source, project_id)
                candidate.update(transport=definition.transport, enabled=definition.enabled, has_credentials=bool(environment or headers), importable=True)
            except (RuntimeFault, ValueError, TypeError):
                candidate.update(importable=False, error={"code": "MCP_IMPORT_UNSUPPORTED", "message": "This registration requires explicit platform configuration"})
            items.append(candidate)
        return {"items": items, "cursor": cursor + limit if cursor + limit < len(entries) else None}

    async def import_codex(self, project_id: str, config_path: Path, server_names: tuple[str, ...]):
        """Import selected business registrations into local storage without connecting."""
        self.host.assert_open()
        require(0 < len(server_names) <= 32 and all(isinstance(name, str) and name for name in server_names), "INVALID_REQUEST", "Choose 1 to 32 exact Codex registration names")
        await self.host.projects.get(project_id)
        entries = await asyncio.to_thread(self.read_codex, config_path)
        imported, skipped, errors = [], [], []
        for name in dict.fromkeys(server_names):
            try:
                require(name in entries, "NOT_FOUND", "The selected Codex registration no longer exists")
                definition, environment, headers = self.codex_definition(name, entries[name], project_id)
                if definition.id in self.configurations:
                    skipped.append({"name": name, "id": definition.id})
                    continue
                references = {"environment_refs": {}, "header_refs": {}, "environment": {key: "" for key, value in environment.items() if not value}}
                for field, values in (("environment_refs", environment), ("header_refs", headers)):
                    for key, value in values.items():
                        if not value:
                            continue
                        identity = new_id("credential")
                        await asyncio.to_thread(self.host.vault.set, identity, value)
                        references[field][key] = "vault:" + identity
                require(not self.host.vault.contains_secret(definition.model_dump(mode="json")), "MCP_IMPORT_UNSUPPORTED", "Move credentials out of public command and URL fields before importing")
                saved = await self.save(definition.model_copy(update=references), expected_revision=0)
                imported.append(saved)
            except (RuntimeFault, ValueError, TypeError) as error:
                fault = error if isinstance(error, RuntimeFault) else RuntimeFault("MCP_IMPORT_UNSUPPORTED", "This registration requires explicit platform configuration")
                errors.append({"name": name, "error": fault.as_dict()})
        return {"imported": imported, "skipped": skipped, "errors": errors}

    async def register_directory(self, directory: Path, project_id: str):
        """Register explicit public connection configuration without connecting."""
        self.host.assert_open()
        directory = Path(directory).expanduser()
        require(directory.is_absolute() and directory.is_dir(), "INVALID_REQUEST", "Choose an existing absolute MCP directory")
        root = directory.resolve()
        require(not root.is_relative_to(self.host.root) and not self.host.root.is_relative_to(root), "PERMISSION_DENIED", "MCP source cannot expose host state")
        source = (root / "mcp.json").resolve()
        require(source.is_relative_to(root) and source.is_file() and source.stat().st_size <= 65536, "INVALID_REQUEST", "MCP directory requires mcp.json within 64 KiB")
        try:
            config = json.loads(await asyncio.to_thread(source.read_text, encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            raise RuntimeFault("INVALID_REQUEST", "Cannot read MCP directory configuration") from None
        require(isinstance(config, dict) and not ({"env", "headers", "secret_values"} & config.keys()), "INVALID_REQUEST", "Use public MCPServer fields and named secret references")
        require(config.get("project_id") in {None, project_id}, "PROJECT_MISMATCH", "MCP directory belongs to another project")
        definition = MCPServer.model_validate({**config, "project_id": project_id, "source_directory": root})
        if definition.transport == "stdio":
            command = definition.command
            if command[0].startswith(("./", ".\\")):
                command = (str((root / command[0]).resolve()), *command[1:])
            definition = definition.model_copy(update={"cwd": (root / (definition.cwd or Path("."))).resolve(), "command": command})
        return await self.save(definition, expected_revision=0)

    async def inspect(self, server_id: str, project_id: str):
        """Observe retained metadata and an existing connection without probing it."""
        saved = await self.get(server_id)
        definition = MCPServer.model_validate(saved["definition"])
        require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "MCP service belongs to another project")
        await self.host.projects.get(project_id)
        catalog = await self.host.store.call(lambda db: one(db, "SELECT json_array_length(tools_json) AS tool_count FROM mcp_catalogs WHERE server_id=? AND revision=?", (server_id, saved["revision"])))
        state = self.connection_state(server_id, saved["revision"])
        active = sum(isinstance(provider := self.host.tools.providers.get(name), UpstreamTool) and provider.server_id == server_id and not self.host.tools.inflight[identity].done() for identity, name in self.host.tools.names.items())
        bound = [agent.id for agent in self.host.definitions.values() if agent.project_id in {None, project_id} and any(binding.server_id == server_id for binding in agent.tools.mcp)]
        return {**saved, "connection_state": state, "tool_count": catalog["tool_count"] if catalog else None, "active_calls": active, "bound_agents": bound}

    def connection_state(self, server_id, revision):
        states = []
        for key, connection in self.connections.items():
            if key[:2] != (server_id, revision) or connection.task.done() or connection.stop.is_set():
                continue
            states.append("connecting" if not connection.ready.done() else "connected" if not connection.ready.cancelled() and connection.ready.exception() is None else "disconnected")
        return "connected" if "connected" in states else "connecting" if "connecting" in states else "disconnected"

    async def calls(self, server_id: str, project_id: str, *, cursor: int = 0, limit: int = 25):
        """Read actual business tool receipts, without loading their result bodies."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded call page")
        saved = await self.get(server_id)
        require(saved["definition"]["project_id"] in {None, project_id}, "PERMISSION_DENIED", "MCP service belongs to another project")
        await self.host.projects.get(project_id)
        values = await self.host.store.call(lambda db: rows(db, "WITH discovered AS (SELECT json_extract(t.value,'$.spec.name') AS alias,json_extract(t.value,'$.upstream_name') AS tool_name FROM mcp_catalogs c,json_each(c.tools_json) t WHERE c.server_id=?) SELECT a.rowid AS position,a.id,a.run_id,a.intent_id,a.name,a.state,a.created_at,a.updated_at,a.error_json,i.agent_id,i.name AS instance_name,t.consumer_id,d.tool_name FROM actions a JOIN discovered d ON d.alias=a.name LEFT JOIN runs r ON r.id=a.run_id LEFT JOIN instances i ON i.id=r.instance_id LEFT JOIN tool_intents t ON t.id=a.intent_id WHERE COALESCE(i.project_id,t.project_id)=? AND (?=0 OR a.rowid<?) ORDER BY a.rowid DESC LIMIT ?", (server_id, project_id, cursor, cursor, limit + 1)))
        return {"items": [{key: value[key] for key in ("position", "id", "run_id", "intent_id", "name", "tool_name", "state", "created_at", "updated_at", "agent_id", "instance_name", "consumer_id")} | {"error": json.loads(value["error_json"]) if value["error_json"] else None} for value in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def disconnect(self, server_id: str, project_id: str):
        """Close this idle service's owned connections; future calls may reconnect."""
        self.host.assert_open()
        observed = await self.inspect(server_id, project_id)
        require(observed["active_calls"] == 0, "MCP_IN_USE", "Observe current calls before disconnecting their connection")
        selected = [(key, value) for key, value in self.connections.items() if key[0] == server_id]
        for key, connection in selected:
            await connection.aclose()
            if self.connections.get(key) is connection:
                self.connections.pop(key)
        await self.host.store.call(lambda db: event(db, "mcp.disconnected", project_id=project_id, data={"server_id": server_id}), transaction=True)
        self.host.notify()
        return {"server_id": server_id, "connection_state": "disconnected"}

    @staticmethod
    def consumer(consumer_id):
        context = operation_context.get() or {}
        reader = consumer_id or context.get("consumer_id")
        require(isinstance(reader, str) and 0 < len(reader) <= 256 and (not context.get("consumer_id") or reader == context["consumer_id"]),
                "PERMISSION_DENIED", "Use one stable controller consumer identity")
        return reader

    @staticmethod
    def intent_scope(db, call_id, project_id, consumer_id):
        record = one(db, "SELECT * FROM tool_intents WHERE id=? AND project_id=? AND consumer_id=?", (call_id, project_id, consumer_id))
        require(record is not None, "PERMISSION_DENIED", "Tool intent belongs to another consumer or project")
        return record

    async def authorize_intent(self, intent_id, name):
        self.host.assert_open()
        intent = await self.host.store.call(lambda db: one(db, "SELECT * FROM tool_intents WHERE id=?", (intent_id,)))
        require(intent is not None and intent["name"] == name,
                "PERMISSION_DENIED", "Controller intent does not grant this tool", call_id=intent_id)
        self.host.assert_project_available(intent["project_id"])
        require(intent["cancel_requested_at"] is None, "CALL_CANCELLED", "Controller intent no longer permits dispatch", call_id=intent_id)
        binding = json.loads(intent["binding_json"])
        saved = await self.get(binding["server_id"])
        definition = MCPServer.model_validate(saved["definition"])
        require(definition.enabled and saved["revision"] == binding["revision"] and definition.project_id in {None, intent["project_id"]},
                "PERMISSION_DENIED", "MCP configuration or project grant changed", call_id=intent_id)
        require(definition.expose_to_codex and (definition.allowed_tools is None or binding["tool_name"] in definition.allowed_tools),
                "PERMISSION_DENIED", "MCP tool is not granted to controller calls", call_id=intent_id)
        require(alias(binding["server_id"], binding["tool_name"], binding["revision"]) == name,
                "PERMISSION_DENIED", "Controller intent has a different discovered tool", call_id=intent_id)

    async def call(self, server_id: str, tool_name: str, arguments: dict, idempotency_key: str, *, project_id: str,
                   consumer_id: str | None = None, wait: bool = True):
        """Try one business tool through the common gateway and original receipt."""
        self.host.assert_project_available(project_id)
        task = asyncio.current_task()
        self.calling[task] = project_id
        try:
            return await self._call_current(server_id, tool_name, arguments, idempotency_key,
                                            project_id=project_id, consumer_id=consumer_id, wait=wait)
        finally:
            self.calling.pop(task, None)

    async def _call_current(self, server_id, tool_name, arguments, idempotency_key, *, project_id, consumer_id, wait):
        self.host.assert_open()
        consumer = self.consumer(consumer_id)
        require(0 < len(idempotency_key) <= 256, "INVALID_REQUEST", "Use one stable tool intent key")
        prior = await self.host.store.call(lambda db: one(db, "SELECT * FROM tool_intents WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (project_id, consumer, idempotency_key)))
        if prior:
            encoded = encode(arguments)
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            binding = json.loads(prior["binding_json"])
            require(prior["input_hash"] == fingerprint and binding["server_id"] == server_id and binding["tool_name"] == tool_name,
                    "IDEMPOTENCY_CONFLICT", "Tool intent key has different input", call_id=prior["id"])
            prepared = PreparedTool(prior["name"], json.loads(encoded), encoded, fingerprint)
            intent = prior
        else:
            saved = await self.get(server_id)
            require(saved["definition"]["enabled"] and saved["definition"]["project_id"] in {None, project_id}, "PERMISSION_DENIED", "MCP service is not currently granted")
            definition = MCPServer.model_validate(saved["definition"])
            require(definition.expose_to_codex and (definition.allowed_tools is None or tool_name in definition.allowed_tools),
                    "PERMISSION_DENIED", "MCP tool is not granted to controller calls")
            catalog = await self.tools(server_id, project_id, revision=saved["revision"])
            tool = next((value for value in catalog["tools"] if value["upstream_name"] == tool_name), None)
            require(tool is not None, "TOOL_UNAVAILABLE", "Select a discovered upstream tool")
            self.register(server_id, saved["revision"], [tool])
            prepared = self.host.tools.prepare(tool["spec"]["name"], arguments)
            require(len(prepared.encoded.encode("utf-8")) <= 1024 * 1024, "INVALID_REQUEST", "Supply large tool inputs by reference")
            binding = {"server_id": server_id, "revision": saved["revision"], "tool_name": tool_name}
            def accept(db):
                previous = one(db, "SELECT * FROM tool_intents WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (project_id, consumer, idempotency_key))
                if previous:
                    original = json.loads(previous["binding_json"])
                    require(previous["input_hash"] == prepared.fingerprint and original["server_id"] == server_id and original["tool_name"] == tool_name,
                            "IDEMPOTENCY_CONFLICT", "Tool intent key has different input", call_id=previous["id"])
                    return previous
                project = one(db, "SELECT archived FROM projects WHERE id=?", (project_id,))
                require(project is not None, "PROJECT_NOT_ACTIVE", "Choose an existing project for new work")
                current = one(db, "SELECT revision,config_json FROM mcp_servers WHERE id=?", (server_id,))
                require(current and current["revision"] == saved["revision"] and json.loads(current["config_json"])["enabled"], "REVISION_CONFLICT", "MCP configuration changed before acceptance")
                identity = new_id("tool-intent")
                db.execute("INSERT INTO tool_intents(id,project_id,consumer_id,name,binding_json,idempotency_key,input_hash,input_json,cancel_requested_at,created_at) VALUES(?,?,?,?,?,?,?,?,NULL,?)", (identity, project_id, consumer, prepared.name, encode(binding), idempotency_key, prepared.fingerprint, encode(self.host.redact(prepared.arguments)), now_ms()))
                event(db, "mcp.call.accepted", intent_id=identity, data={"server_id": server_id, "tool_name": tool_name})
                return one(db, "SELECT * FROM tool_intents WHERE id=?", (identity,))
            intent = await self.host.store.call(accept, transaction=True)
        action = await self.host.store.call(lambda db: one(db, "SELECT * FROM actions WHERE intent_id=?", (intent["id"],)))
        if action is None or action["state"] == "intent" and action["id"] not in self.host.tools.inflight:
            if intent["name"] not in self.host.tools.providers:
                original = json.loads(intent["binding_json"])
                catalog = await self.tools(original["server_id"], project_id, revision=original["revision"])
                self.register(original["server_id"], original["revision"], catalog["tools"])
            if prepared.name != intent["name"]:
                prepared = PreparedTool(intent["name"], prepared.arguments, prepared.encoded, prepared.fingerprint)
            # Publication happens after the durable action intent. Only the gateway
            # owns the worker; the transport may return or disappear independently.
            action = await self.host.tools.invoke_intent(intent["id"], prepared, intent["id"] + ":invoke", wait=False)
        if wait and action["id"] in self.host.tools.inflight:
            try:
                await self.host.tools._result(self.host.tools.inflight[action["id"]], action, self.host.tools.providers[action["name"]].describe())
            except RuntimeFault:
                # The original error/uncertain outcome is retained in the same
                # action and returned below, rather than claimed as completion.
                pass
        self.host.notify()
        return await self.call_result(intent["id"], project_id=project_id, consumer_id=consumer)

    async def call_result(self, call_id: str, *, project_id: str, consumer_id: str | None = None, version: str | None = None):
        """Read a controller tool intent and its original action without dispatch."""
        self.host.assert_open()
        consumer = self.consumer(consumer_id)
        def read(db):
            intent = self.intent_scope(db, call_id, project_id, consumer)
            action = one(db, "SELECT * FROM actions WHERE intent_id=?", (call_id,))
            binding = json.loads(intent["binding_json"])
            outcome = hashlib.sha256(encode([action["state"], action["result_json"], action["error_json"]]).encode("utf-8")).hexdigest() if action else None
            require(version is None or version == outcome, "SOURCE_VERSION_CHANGED", "Original tool outcome changed; inspect its current evidence")
            return {"id": call_id, "project_id": project_id, "consumer_id": consumer, **binding,
                    "version": outcome,
                    "action_id": action["id"] if action else None, "state": action["state"] if action else "failed" if intent["cancel_requested_at"] else "intent",
                    "cancel_requested_at": intent["cancel_requested_at"], "created_at": intent["created_at"],
                    "arguments": json.loads(intent["input_json"]),
                    "result": json.loads(action["result_json"]) if action and action["result_json"] else None,
                    "error": json.loads(action["error_json"]) if action and action["error_json"] else {"code": "ACTION_NOT_STARTED", "safe_message": "Cancelled before any action was accepted"} if action is None and intent["cancel_requested_at"] else None}
        result = await self.host.store.call(read)
        result["in_flight"] = self.host.tools.has_inflight(call_id)
        result["stop_confirmed"] = result["state"] in {"completed", "failed"} and not result["in_flight"]
        return result

    async def cancel_call(self, call_id: str, *, project_id: str, consumer_id: str | None = None):
        """Request cancellation of the original intent; retain uncertain effects."""
        self.host.assert_open()
        consumer = self.consumer(consumer_id)
        def cancel(db):
            self.intent_scope(db, call_id, project_id, consumer)
            action = one(db, "SELECT state FROM actions WHERE intent_id=?", (call_id,))
            if action and action["state"] in {"completed", "failed"}:
                return
            db.execute("UPDATE tool_intents SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE id=?", (now_ms(), call_id))
            event(db, "mcp.call.cancel_requested", intent_id=call_id)
        await self.host.store.call(cancel, transaction=True)
        await self.host.tools.cancel(call_id)
        self.host.notify()
        return await self.call_result(call_id, project_id=project_id, consumer_id=consumer)

    async def confirm_call(self, call_id: str, idempotency_key: str, evidence: str, outcome: Literal["completed", "not_applied"], *,
                           project_id: str, consumer_id: str | None = None, result: dict | None = None):
        """Record external verification through the shared action confirmation."""
        self.host.assert_open()
        original = await self.call_result(call_id, project_id=project_id, consumer_id=consumer_id)
        require(original["action_id"], "ACTION_NOT_CONFIRMABLE", "This intent has no dispatched action")
        value = await self.host.tools.confirm(original["action_id"], project_id, idempotency_key, evidence, outcome, result=result, intent_id=call_id)
        self.host.on_intent_settle(call_id)
        return value

    async def get(self, server_id: str):
        record = await self.host.store.call(lambda db: one(db, "SELECT * FROM mcp_servers WHERE id=?", (server_id,)))
        require(record, "NOT_FOUND", "MCP service does not exist")
        return {"definition": json.loads(record["config_json"]), "revision": record["revision"]}

    async def list(self, project_id: str, *, cursor: int = 0, limit: int = 25, include_removed: bool = False):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded page")
        values = await self.host.store.call(lambda db: rows(db, "SELECT s.rowid AS position,s.*,(SELECT json_array_length(tools_json) FROM mcp_catalogs c WHERE c.server_id=s.id AND c.revision=s.revision) AS tool_count FROM mcp_servers s WHERE (s.project_id=? OR s.project_id IS NULL) AND s.rowid>? AND (? OR COALESCE(json_extract(s.config_json,'$.removed'),0)=0) ORDER BY s.rowid LIMIT ?", (project_id, cursor, int(include_removed), limit + 1)))
        active, bound = {}, {}
        for identity, name in self.host.tools.names.items():
            provider = self.host.tools.providers.get(name)
            if isinstance(provider, UpstreamTool) and not self.host.tools.inflight[identity].done():
                active[provider.server_id] = active.get(provider.server_id, 0) + 1
        for agent in self.host.definitions.values():
            if agent.project_id in {None, project_id}:
                for binding in agent.tools.mcp:
                    bound.setdefault(binding.server_id, []).append(agent.id)
        return {"items": [{"position": value["position"], "definition": json.loads(value["config_json"]), "revision": value["revision"], "connection_state": self.connection_state(value["id"], value["revision"]), "tool_count": value["tool_count"], "active_calls": active.get(value["id"], 0), "bound_agents": bound.get(value["id"], [])} for value in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def schema(self):
        return MCPServer.model_json_schema()

    async def set_credential(self, server_id: str, project_id: str, target: Literal["header", "environment"], name: str,
                             value: str, expected_revision: int):
        """Rotate one MCP secret reference without returning its value."""
        self.host.assert_open()
        import re
        require(0 < len(name) <= 256 and len(value) <= 16384, "INVALID_REQUEST", "Use a bounded secret name and value")
        require(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+" if target == "header" else r"[A-Za-z_][A-Za-z0-9_]*", name), "INVALID_REQUEST", "Secret entry name is invalid")
        require(target != "header" or "\r" not in value and "\n" not in value, "INVALID_REQUEST", "HTTP credentials cannot contain line breaks")
        saved = await self.get(server_id)
        definition = MCPServer.model_validate(saved["definition"])
        require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "MCP service belongs to another project")
        await self.host.projects.get(project_id)
        require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "MCP configuration changed")
        require((target == "header") == (definition.transport == "streamable_http"), "INVALID_REQUEST", "Choose a secret entry for this connection transport")
        field = "header_refs" if target == "header" else "environment_refs"
        references = dict(getattr(definition, field))
        reference = None
        if value:
            identity = new_id("credential")
            await asyncio.to_thread(self.host.vault.set, identity, value)
            reference = references[name] = "vault:" + identity
        else:
            references.pop(name, None)
        updated = definition.model_copy(update={field: references})
        result = await self.save(updated, expected_revision=expected_revision)
        return {"server_id": server_id, "target": target, "name": name, "configured": bool(value), "credential_ref": reference, "revision": result["revision"]}

    async def tools(self, server_id: str, project_id: str, *, revision: int | None = None):
        """Read retained discovery without contacting or invoking the server."""
        saved = await self.get(server_id)
        require(saved["definition"]["project_id"] in {None, project_id}, "PERMISSION_DENIED", "MCP service belongs to another project")
        await self.host.projects.get(project_id)
        revision = saved["revision"] if revision is None else revision
        record = await self.host.store.call(lambda db: one(db, "SELECT tools_json FROM mcp_catalogs WHERE server_id=? AND revision=?", (server_id, revision)))
        require(record, "MCP_NOT_DISCOVERED", "Discover this service revision first")
        return {"server_id": server_id, "project_id": project_id, "revision": revision, "tools": json.loads(record["tools_json"])}

    @staticmethod
    def controller_scope(project_id, consumer_id):
        return encode(["controller", project_id, consumer_id])

    async def connection(self, server_id, revision, *, consumer=None):
        self.host.assert_open()
        saved = await self.get(server_id)
        definition = MCPServer.model_validate(saved["definition"])
        require(definition.enabled and saved["revision"] == revision, "MCP_CONFIGURATION_CHANGED", "MCP service was disabled or changed; explicitly resume with refreshed capabilities")
        if consumer is None:
            context = operation_context.get() or {}
            consumer = self.controller_scope(definition.project_id, context.get("consumer_id") or "local")
        key = server_id, revision, "shared" if definition.session_scope == "shared" else consumer
        connection = self.connections.get(key)
        if connection is None or connection.task.done() or connection.stop.is_set():
            connection = self.connections[key] = Connection(self, definition, revision)
        try:
            return await asyncio.shield(connection.ready)
        except RuntimeFault:
            raise
        except Exception as error:
            raise RuntimeFault("MCP_CONNECTION_FAILED", "Business MCP connection could not initialize", details={"cause": type(error).__name__}) from error

    def register(self, server_id, revision, catalog):
        for value in catalog:
            name = value["spec"]["name"]
            existing = self.host.tools.providers.get(name)
            if existing is None:
                self.host.tools.register(UpstreamTool(self, server_id, revision, value))
            else:
                require(isinstance(existing, UpstreamTool) and existing.server_id == server_id and existing.revision == revision and existing.upstream_name == value["upstream_name"], "TOOL_EXISTS", "A discovered alias belongs to another tool provider")
                require(existing.describe().model_dump(mode="json") == value["spec"], "MCP_CATALOG_CHANGED", "MCP metadata changed; save a new service revision before using it")

    async def discover(self, server_id: str, project_id: str, *, consumer_id: str | None = None):
        """Initialize only this configured business connection and retain its schema."""
        reader = self.consumer(consumer_id) if consumer_id is not None or (operation_context.get() or {}).get("consumer_id") else "local"
        return await self._discover(server_id, project_id, self.controller_scope(project_id, reader))

    async def _discover(self, server_id, project_id, consumer):
        self.host.assert_project_available(project_id)
        task = asyncio.current_task()
        self.discovering[task] = project_id
        try:
            return await self._discover_current(server_id, project_id, consumer)
        finally:
            self.discovering.pop(task, None)

    async def _discover_current(self, server_id, project_id, consumer):
        saved = await self.get(server_id)
        definition, revision = MCPServer.model_validate(saved["definition"]), saved["revision"]
        require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "MCP service belongs to another project")
        await self.host.projects.get(project_id)
        try:
            connection = await self.connection(server_id, revision, consumer=consumer)
            catalog, cursor, seen = [], None, set()
            async with asyncio.timeout(definition.timeout_seconds):
                while True:
                    page = await connection.session.list_tools(cursor=cursor)
                    for tool in page.tools:
                        require(tool.name not in seen, "MCP_PROTOCOL_ERROR", "MCP service returned a duplicate tool")
                        seen.add(tool.name)
                        # Read-only annotations are advisory, so every business MCP
                        # invocation retains uncertain side-effect semantics.
                        spec = ToolSpec(name=alias(server_id, tool.name, revision), description=tool.description or tool.name,
                            input_schema=tool.inputSchema,
                            output_schema={"type": "object", "properties": {"isError": {"type": "boolean"}, "content": {"type": "array", "items": {"type": "object"}},
                                "structured_content": {"type": ["object", "null"]}}, "required": ["isError", "content", "structured_content"], "additionalProperties": False},
                            side_effects="external", cancellation="none", timeout_seconds=definition.timeout_seconds)
                        catalog.append({"upstream_name": tool.name, "spec": spec.model_dump(mode="json")})
                    if not page.nextCursor:
                        break
                    require(page.nextCursor != cursor, "MCP_PROTOCOL_ERROR", "MCP discovery cursor did not advance")
                    cursor = page.nextCursor
            require(not {"platform_guide", "platform_call"} <= seen, "CONTROL_BRIDGE_FORBIDDEN", "Platform control tools cannot be bound as business tools")
            require(not self.host.vault.contains_secret(catalog), "MCP_CATALOG_CONTAINS_SECRET", "MCP tool metadata contains protected credentials; correct the server declaration")
            try:
                self.register(server_id, revision, catalog)
            except SchemaError:
                raise RuntimeFault("MCP_SCHEMA_INVALID", "MCP tool declaration is not valid JSON Schema") from None
            encoded = encode(catalog)
            def save(db):
                current = one(db, "SELECT revision FROM mcp_servers WHERE id=?", (server_id,))
                require(current["revision"] == revision, "REVISION_CONFLICT", "MCP configuration changed during discovery")
                previous = one(db, "SELECT tools_json FROM mcp_catalogs WHERE server_id=? AND revision=?", (server_id, revision))
                require(not previous or previous["tools_json"] == encoded, "MCP_CATALOG_CHANGED", "MCP tools changed; save a new service revision before using them")
                db.execute("INSERT OR IGNORE INTO mcp_catalogs VALUES(?,?,?,?)", (server_id, revision, encoded, now_ms()))
            await self.host.store.call(save, transaction=True)
            return {"server_id": server_id, "project_id": project_id, "revision": revision, "tools": catalog}
        finally:
            self.after_settle()

    async def bind(self, context):
        context.mcp_tools = {}
        if context.instance["role"] != "main":
            return
        for binding in context.definition.tools.mcp:
            saved = await self.get(binding.server_id)
            definition = MCPServer.model_validate(saved["definition"])
            require(definition.enabled and definition.project_id in {None, context.instance["project_id"]}, "PERMISSION_DENIED", "MCP service is not granted to this project")
            record = await self.host.store.call(lambda db: one(db, "SELECT * FROM run_mcp_bindings WHERE run_id=? AND server_id=?", (context.run["id"], binding.server_id)))
            revision = record["revision"] if record else saved["revision"]
            require(revision == saved["revision"], "MCP_CONFIGURATION_CHANGED", "Explicitly refresh capabilities after an MCP configuration change")
            catalog = await self.host.store.call(lambda db: one(db, "SELECT tools_json FROM mcp_catalogs WHERE server_id=? AND revision=?", (binding.server_id, revision)))
            if catalog is None:
                # Discovery uses the same instance-owned upstream session as calls.
                await self._discover(binding.server_id, context.instance["project_id"], encode(["instance", context.instance["id"]]))
                catalog = await self.host.store.call(lambda db: one(db, "SELECT tools_json FROM mcp_catalogs WHERE server_id=? AND revision=?", (binding.server_id, revision)))
            selected = [value for value in json.loads(catalog["tools_json"]) if (binding.tools is None or value["upstream_name"] in binding.tools)
                        and (definition.allowed_tools is None or value["upstream_name"] in definition.allowed_tools)]
            require(binding.tools is None or set(binding.tools) <= {value["upstream_name"] for value in selected}, "TOOL_UNAVAILABLE", "A configured MCP tool is absent from discovery")
            self.register(binding.server_id, revision, selected)
            await self.host.store.call(lambda db: db.execute("INSERT OR IGNORE INTO run_mcp_bindings VALUES(?,?,?)", (context.run["id"], binding.server_id, revision)), transaction=True)
            context.mcp_tools.update({value["spec"]["name"]: self.host.tools.providers[value["spec"]["name"]] for value in selected})

    def names(self, context):
        current = {value.server_id: value for value in self.host.definitions[context.instance["agent_id"]].tools.mcp}
        return [name for name, tool in context.mcp_tools.items() if tool.server_id in current and self.current_grant(context, tool) and
                (current[tool.server_id].tools is None or tool.upstream_name in current[tool.server_id].tools)]

    def current_grant(self, context, tool):
        current = self.configurations.get(tool.server_id)
        return current and current[0].enabled and current[1] == tool.revision and current[0].project_id in {None, context.instance["project_id"]} and (
            current[0].allowed_tools is None or tool.upstream_name in current[0].allowed_tools)

    async def authorize(self, context, name):
        require(name in self.names(context), "PERMISSION_DENIED", "MCP tool is no longer granted")
        tool = context.mcp_tools[name]
        saved = await self.get(tool.server_id)
        definition = MCPServer.model_validate(saved["definition"])
        require(definition.enabled and saved["revision"] == tool.revision and definition.project_id in {None, context.instance["project_id"]},
                "PERMISSION_DENIED", "MCP configuration or project grant changed")

    async def aclose(self):
        if self.retirement is not None:
            await asyncio.gather(self.retirement, return_exceptions=True)
        await asyncio.gather(*(connection.aclose() for connection in self.connections.values()))
        self.connections.clear()
