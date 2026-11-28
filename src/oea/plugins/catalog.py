"""Explicit catalog refresh and cached discovery; no execution or package installation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import urllib.request

from oea.contracts.errors import RuntimeFault, require
from oea.plugins.contracts import OFFICIAL
from oea.storage.persistence import encode, event, one


class PluginCatalog:
    def __init__(self, store, root, contains_secret, notify, installed):
        self.store, self.root, self.contains_secret = store, root, contains_secret
        self.notify, self.installed = notify, installed
        self.catalog_sources = {}

    async def open(self):
        saved = await self.store.call(lambda db: one(db, "SELECT value FROM metadata WHERE key='plugin_catalog_sources'"))
        self.catalog_sources = json.loads(saved["value"]) if saved else {}

    async def list(self, *, cursor: int = 0, limit: int = 25):
        require(type(cursor) is int and cursor >= 0 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded plugin catalog page")
        values = [{"id": identity, **spec, "version": "1.0.0", "source": "official:" + identity, "official": True, "catalog_name": "OEA"} for identity, spec in OFFICIAL.items()]
        for source in self.catalog_sources.values():
            values.extend({**item, "official": False, "catalog_id": source["id"], "catalog_name": source["name"]} for item in source["items"])
        for item in values:
            installed = self.installed().get(item["id"])
            item["installed"] = bool(installed and not installed["removed"])
            item["enabled"] = bool(installed and installed["enabled"] and not installed["removed"])
        return {"items": values[cursor:cursor + limit], "cursor": cursor + limit if len(values) > cursor + limit else None}

    async def sources(self):
        return {"items": [{key: value for key, value in source.items() if key != "items"} | {"entry_count": len(source["items"])} for source in self.catalog_sources.values()]}

    def _read_catalog(self, source):
        parsed = urlsplit(source)
        remote = parsed.scheme == "https"
        if remote:
            require(parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment, "INVALID_PLUGIN_SOURCE", "Use a credential-free HTTPS catalog URL")
            with urllib.request.urlopen(source, timeout=30) as response:
                body = response.read(2 * 1024 * 1024 + 1)
            base = None
        else:
            path = Path(source).expanduser().resolve()
            require(path.is_file() and not path.is_relative_to(self.root), "INVALID_PLUGIN_SOURCE", "Choose a catalog JSON file outside private host state")
            body = path.read_bytes()
            base = path.parent
        require(len(body) <= 2 * 1024 * 1024 and not self.contains_secret(body.decode("utf-8")), "INVALID_PLUGIN_CATALOG", "Catalog is too large or contains configured credentials")
        catalog = json.loads(body)
        entries = catalog.get("plugins") if isinstance(catalog, dict) else None
        require(isinstance(entries, list) and len(entries) <= 1000, "INVALID_PLUGIN_CATALOG", "A catalog needs a bounded plugins array")
        values = []
        for entry in entries:
            require(isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("source"), str), "INVALID_PLUGIN_CATALOG", "Catalog entries need name and source strings")
            target = entry["source"]
            if target.startswith("official:"):
                require(target[9:] in OFFICIAL, "INVALID_PLUGIN_CATALOG", "Unknown official source")
            elif target.startswith("https://"):
                url, separator, commit = target.rpartition("#")
                location = urlsplit(url)
                require(separator and re.fullmatch(r"[0-9a-f]{40}", commit) and location.hostname and not location.username and not location.password and not location.query,
                        "INVALID_PLUGIN_CATALOG", "Git entries require credential-free HTTPS URLs pinned to a full commit")
            else:
                require(not remote, "INVALID_PLUGIN_CATALOG", "Remote catalogs cannot select local paths")
                path = (base / target).resolve()
                require(path.is_relative_to(base), "INVALID_PLUGIN_CATALOG", "Local catalog entries must remain within their catalog root")
                target = str(path)
            values.append({"id": str(entry.get("id", entry["name"])), "name": entry["name"], "source": target,
                           "description": str(entry.get("description", "")), "version": str(entry.get("version", ""))})
        return values

    async def _source_write(self, identity, value, expected_revision):
        def write(db):
            record = one(db, "SELECT value FROM metadata WHERE key='plugin_catalog_sources'")
            sources = json.loads(record["value"]) if record else {}
            current = sources.get(identity)
            require((current["revision"] if current else 0) == expected_revision, "REVISION_CONFLICT", "Plugin catalog changed")
            if value is None:
                sources.pop(identity)
            else:
                sources[identity] = {**value, "revision": expected_revision + 1}
            db.execute("INSERT INTO metadata VALUES('plugin_catalog_sources',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encode(sources),))
            event(db, "plugin.saved", data={"id": identity, "revision": expected_revision + 1})
            return sources
        self.catalog_sources = await self.store.call(write, transaction=True)
        self.notify()
        return self.catalog_sources.get(identity, {"id": identity, "removed": True})

    async def add_source(self, source: str, name: str):
        require(0 < len(name.strip()) <= 120, "INVALID_PLUGIN_CATALOG", "Use a bounded catalog name")
        identity = "catalog_" + hashlib.sha256(source.encode()).hexdigest()[:24]
        current = self.catalog_sources.get(identity)
        try:
            items = await asyncio.to_thread(self._read_catalog, source)
        except (OSError, ValueError) as error:
            raise RuntimeFault("PLUGIN_CATALOG_UNAVAILABLE", "Could not read the catalog", details={"cause": type(error).__name__}) from error
        result = await self._source_write(identity, {"id": identity, "source": source, "name": name.strip(), "items": items}, current["revision"] if current else 0)
        return {key: value for key, value in result.items() if key != "items"} | {"entry_count": len(items)}

    async def refresh_source(self, source_id: str):
        source = self.catalog_sources.get(source_id)
        require(source is not None, "NOT_FOUND", "Plugin catalog is unavailable")
        return await self.add_source(source["source"], source["name"])

    async def remove_source(self, source_id: str):
        source = self.catalog_sources.get(source_id)
        require(source is not None, "NOT_FOUND", "Plugin catalog is unavailable")
        return await self._source_write(source_id, None, source["revision"])
