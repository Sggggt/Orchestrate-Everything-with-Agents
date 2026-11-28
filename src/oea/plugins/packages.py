"""Filesystem-only package preparation and export, without Host or database access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
import zipfile

from oea.contracts.errors import require
from oea.mcp.upstream import MCPServer
from oea.plugins.contracts import PLUGIN_SCHEMA, MCP_SCHEMA, NAMESPACE, component_id, package_json as encode


class PluginPackages:
    def __init__(self, root, contains_secret):
        self.root, self.contains_secret = root, contains_secret

    @staticmethod
    def manifest(root):
        path = (root / "plugin.json").resolve()
        require(path.is_relative_to(root) and path.is_file(), "INVALID_PLUGIN", "A plugin needs root plugin.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        require(isinstance(value, dict) and value.get("$schema") == PLUGIN_SCHEMA, "INVALID_PLUGIN", "Use the supported Agent Plugins schema")
        name = value.get("name", "")
        require(isinstance(name, str) and bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?", name)) and "--" not in name and ".." not in name,
                "INVALID_PLUGIN", "Use a portable plugin name")
        text_fields = ("version", "description", "homepage", "repository", "license")
        require(all(key not in value or isinstance(value[key], str) for key in text_fields), "INVALID_PLUGIN", "Manifest metadata fields must be strings")
        author = value.get("author", {})
        require(isinstance(author, dict) and set(author) <= {"name", "email", "url"} and all(isinstance(item, str) for item in author.values()), "INVALID_PLUGIN", "Author metadata must contain supported string fields")
        keywords = value.get("keywords", [])
        require(isinstance(keywords, list) and all(isinstance(item, str) for item in keywords), "INVALID_PLUGIN", "Keywords must be strings")
        extensions = value.get("extensions", {})
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        require(isinstance(extensions.get(NAMESPACE, {}), dict), "INVALID_PLUGIN", "OEA extension must be an object")
        own = dict(extensions.get(NAMESPACE, {}))
        for key in ("mcp_refs", "skill_refs", "operations"):
            require(isinstance(own.get(key, []), list) and all(isinstance(item, str) for item in own.get(key, [])), "INVALID_PLUGIN", "OEA component references must be lists of strings")
        require(isinstance(own.get("configuration_required", {}), dict), "INVALID_PLUGIN", "Connection configuration declarations must be an object")
        allowed = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords", "extensions"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            own["issues"] = [{"field": key, "reason": "UNSUPPORTED_MANIFEST_FIELD"} for key in unknown]
        extensions[NAMESPACE] = own
        value = {key: item for key, item in value.items() if key in allowed and key != "extensions"} | {"extensions": extensions}
        # Installation validates the package boundary once, not on every model turn.
        for path in root.rglob("*"):
            require(not path.is_symlink() and not path.is_junction() and path.resolve().is_relative_to(root), "INVALID_PLUGIN_PATH", "Plugin components must remain inside their package")
        return value

    def prepare(self, source):
        staging = self.root / "plugin-staging"
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as temporary:
            temporary = Path(temporary)
            path = Path(source).expanduser()
            if path.is_dir():
                origin = path.resolve()
                managed = origin.is_relative_to(self.root / "plugin-exports") or origin.is_relative_to(self.root / "plugins" / "packages")
                require(managed or not origin.is_relative_to(self.root) and not self.root.is_relative_to(origin), "INVALID_PLUGIN_PATH", "Import a package outside private host state")
                self.manifest(origin)
                shutil.copytree(origin, temporary / "package", ignore=shutil.ignore_patterns(".git", "__pycache__"))
            elif path.is_file() and zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    for entry in archive.infolist():
                        target = (temporary / "package" / entry.filename).resolve()
                        require(target.is_relative_to(temporary / "package") and (entry.external_attr >> 16) & 0o170000 != 0o120000,
                                "INVALID_PLUGIN_PATH", "Archive entries must stay in the package")
                    archive.extractall(temporary / "package")
            else:
                url, separator, commit = source.rpartition("#")
                parsed = urlsplit(url)
                require(separator and re.fullmatch(r"[0-9a-f]{40}", commit) and parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.query,
                        "INVALID_PLUGIN_SOURCE", "Use a local directory, ZIP, or HTTPS Git URL pinned with #COMMIT")
                subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none", "--", url, str(temporary / "package")], check=True, capture_output=True, timeout=120)
                subprocess.run(["git", "-C", str(temporary / "package"), "checkout", "--detach", commit], check=True, capture_output=True, timeout=60)
                shutil.rmtree(temporary / "package" / ".git")
            root = (temporary / "package").resolve()
            manifest = self.manifest(root)
            fingerprint = hashlib.sha256()
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    require(not self.contains_secret(item.read_text(encoding="utf-8", errors="ignore")), "PLUGIN_SECRET", "Packages cannot contain configured credentials")
                    fingerprint.update(item.relative_to(root).as_posix().encode())
                    fingerprint.update(item.read_bytes())
            target = self.root / "plugins" / "packages" / fingerprint.hexdigest()
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root), str(target))
            return target, manifest

    def server(self, root, plugin_id, revision, name, entry, project_id):
        data = self.root / "plugin-data" / component_id(plugin_id, name, 0)
        substitutions = {"PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(data)}
        def expand(value):
            require(isinstance(value, str), "INVALID_PLUGIN", "MCP arguments and environment values must be text")
            return re.sub(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}", lambda match: substitutions[match[1]], value)
        base = {"id": component_id(plugin_id, name, revision), "name": name, "project_id": project_id, "source_directory": root, "plugin_owner": plugin_id}
        require(isinstance(entry.get("args", []), list) and all(isinstance(value, str) for value in entry.get("args", [])), "INVALID_PLUGIN", "MCP args must be a string array")
        require(isinstance(entry.get("env", {}), dict) and all(isinstance(value, str) for value in entry.get("env", {}).values()), "INVALID_PLUGIN", "MCP environment must contain strings")
        transport = entry.get("type")
        if transport == "stdio":
            require(set(entry) <= {"type", "command", "args", "env", "cwd"}, "INVALID_PLUGIN", "Unknown stdio MCP fields")
            command = entry.get("command")
            require(isinstance(command, str) and command and not Path(command).is_absolute(), "INVALID_PLUGIN", "Command must be a bare executable or plugin-relative token")
            if command.startswith("./"):
                executable = (root / command).resolve()
                require(executable.is_relative_to(root), "INVALID_PLUGIN_PATH", "Executable escapes its package")
                command = str(executable)
            else:
                require("/" not in command and "\\" not in command, "INVALID_PLUGIN", "Relative commands start with ./")
            environment = entry.get("env", {})
            require(not set(environment) & set(substitutions), "INVALID_PLUGIN", "Reserved plugin variables cannot be overridden")
            environment = {key: expand(value) for key, value in environment.items()} | substitutions
            raw_cwd = entry.get("cwd", "${PLUGIN_ROOT}")
            require(isinstance(raw_cwd, str) and bool(re.match(r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))", raw_cwd)), "INVALID_PLUGIN_PATH", "Use a portable working directory")
            cwd = Path(expand(raw_cwd))
            cwd = (root / cwd).resolve() if not cwd.is_absolute() else cwd.resolve()
            require(cwd.is_relative_to(root) or cwd.is_relative_to(data), "INVALID_PLUGIN_PATH", "MCP working directory escapes plugin roots")
            data.mkdir(parents=True, exist_ok=True)
            return MCPServer(**base, command=(command, *(expand(value) for value in entry.get("args", []))), cwd=cwd, environment=environment)
        require(transport == "streamable-http", "PLUGIN_COMPONENT_UNSUPPORTED", "Supported MCP transports are stdio and streamable-http")
        require(set(entry) <= {"type", "url", "headers"}, "INVALID_PLUGIN", "Unknown HTTP MCP fields")
        headers = entry.get("headers", {})
        require(isinstance(headers, dict) and all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()), "INVALID_PLUGIN", "HTTP headers must be strings")
        require(not {key.lower() for key in headers} & {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}, "PLUGIN_CREDENTIAL_REQUIRED", "Bind authentication through the installed OEA credential configuration")
        parsed = urlsplit(entry.get("url", ""))
        require(parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}, "INVALID_PLUGIN", "Remote MCP services require HTTPS")
        return MCPServer(**base, transport="streamable_http", url=entry["url"], headers=headers)

    def validate_skill(self, origin):
        for path in origin.rglob("*"):
            require(not path.is_symlink() and not path.is_junction() and path.resolve().is_relative_to(origin), "INVALID_PLUGIN_PATH", "Skill files must stay within their source")
            if path.is_file():
                require(not self.contains_secret(path.read_text(encoding="utf-8", errors="ignore")), "PLUGIN_SECRET", "Skill export contains credentials")

    def merge_server(self, desired, configured, previous_root, name, previous_manifest):
        """Carry actual user edits forward while adopting new package defaults."""
        previous_root = Path(previous_root)
        original = json.loads((previous_root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][name]
        baseline = self.server(previous_root, desired.plugin_owner, previous_root.name, name, original, desired.project_id)
        if "mcp:" + name in previous_manifest.get("extensions", {}).get(NAMESPACE, {}).get("configuration_required", {}):
            baseline = baseline.model_copy(update={"enabled": False})
        old, current, merged = (item.model_dump(mode="json") for item in (baseline, configured, desired))
        old_root, new_root = str(previous_root), str(desired.source_directory)
        def rebase(value):
            if isinstance(value, str):
                return value.replace(old_root, new_root)
            if isinstance(value, list):
                return [rebase(item) for item in value]
            return value
        for field in set(current) - {"id", "project_id", "source_directory", "plugin_owner", "command"}:
            if field in {"environment", "environment_refs", "headers", "header_refs"}:
                values = dict(merged[field])
                for key in set(old[field]) | set(current[field]):
                    if key not in current[field]:
                        values.pop(key, None)
                    elif key not in old[field] or old[field][key] != current[field][key]:
                        values[key] = rebase(current[field][key])
                merged[field] = values
            elif current[field] != old[field]:
                merged[field] = rebase(current[field])
        if current["command"]:
            command = list(merged["command"])
            if current["command"][:1] != old["command"][:1]:
                command[:1] = rebase(current["command"][:1])
            if current["command"][1:] != old["command"][1:]:
                command[1:] = rebase(current["command"][1:])
            merged["command"] = command
        elif current["command"] != old["command"]:
            merged["command"] = []
        return MCPServer.model_validate(merged)

    def build(self, name, configurations, material, project_id, idempotency_key, *, portable=False):
        require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?", name) and "--" not in name and ".." not in name, "INVALID_PLUGIN", "Choose a portable plugin name")
        export = self.root / "plugin-exports" / hashlib.sha256(encode([project_id, idempotency_key]).encode()).hexdigest()
        target = export / "package"
        manifest = {"$schema": PLUGIN_SCHEMA, "name": name, "version": "1.0.0"}
        server_ids = [config.id for config in configurations]
        skill_ids = [config["id"] for config in material]
        servers, required = {}, {}
        if not portable:
            manifest["extensions"] = {NAMESPACE: {"mcp_refs": server_ids, "skill_refs": skill_ids}}
        for index, config in enumerate(configurations, 1):
            if not portable:
                continue
            component = "connection-" + str(index)
            if config.transport == "stdio":
                servers[component] = {"type": "stdio", "command": Path(config.command[0]).name}
                required["mcp:" + component] = ["command", "args", "cwd", "environment", "credentials"]
            else:
                servers[component] = {"type": "streamable-http", "url": "https://example.invalid/mcp"}
                required["mcp:" + component] = ["url", "headers", "credentials"]
        if portable:
            manifest["extensions"] = {NAMESPACE: {"configuration_required": required}}
        require(not self.contains_secret(encode([manifest, servers])), "PLUGIN_SECRET", "Export metadata contains credentials")
        fingerprint = hashlib.sha256(encode([manifest, servers, skill_ids]).encode()).hexdigest()
        receipt = export / "receipt.json"
        if receipt.exists():
            prior = json.loads(receipt.read_text(encoding="utf-8"))
            require(prior["fingerprint"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Export key has different content")
            return prior
        target.mkdir(parents=True, exist_ok=False)
        (target / "plugin.json").write_text(encode(manifest), encoding="utf-8")
        (target / "mcp.json").write_text(encode({"$schema": MCP_SCHEMA, "mcpServers": servers}), encoding="utf-8")
        for config in material if portable else []:
            origin = Path(config["path"]).resolve()
            self.validate_skill(origin)
            shutil.copytree(origin, target / "skills" / config["id"])
        result = {"path": str(target), "fingerprint": fingerprint, "name": name, "portable": portable,
                  "connection_configuration_required": bool(required),
                  "credential_configuration_required": any(config.environment_refs or config.header_refs for config in configurations)}
        receipt.write_text(encode(result), encoding="utf-8")
        return result
