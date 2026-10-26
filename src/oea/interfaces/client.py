"""Version-two client for an existing local host; never starts another host."""

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from oea.contracts.configuration import PROTOCOL_VERSION
from oea.contracts.errors import RuntimeFault, require
from oea.models.api import sse_data
from oea.storage.persistence import new_id


class PlatformClient:
    def __init__(self, url, token, *, consumer_id=None, transport=None, agent_name=None):
        parsed = urlsplit(url)
        require(parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in {"", "/"},
                "INVALID_HOST_URL", "Connect to an explicit local host origin")
        self.url, self.token = url.rstrip("/"), token
        self.consumer_id = consumer_id or new_id("consumer")
        self.agent_name = agent_name
        self.transport, self.client, self.loop = transport, None, None
        self.opened = False

    @classmethod
    def from_data_dir(cls, data_dir=None, **options):
        from oea.workspace.paths import resolve_data_dir
        root = resolve_data_dir(data_dir)
        try:
            metadata = json.loads((root / "client.json").read_text(encoding="utf-8"))
            token = (root / "client-token").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise RuntimeFault("HOST_NOT_CONFIGURED", "Start 'oea serve' before connecting to this OEA home") from None
        except (ValueError, TypeError):
            raise RuntimeFault("INVALID_HOST_METADATA", "Saved host metadata is invalid") from None
        require(isinstance(metadata, dict) and isinstance(metadata.get("url"), str) and bool(token),
                "INVALID_HOST_METADATA", "Saved host URL or authentication is unavailable")
        return cls(metadata["url"], token, **options)

    async def __aenter__(self):
        require(not self.opened, "CLIENT_ALREADY_OPEN", "Client is already open")
        self.loop = asyncio.get_running_loop()
        self.client = httpx.AsyncClient(base_url=self.url, follow_redirects=False, trust_env=False, transport=self.transport,
            headers={"Authorization": "Bearer " + self.token, "X-OEA-Protocol-Version": PROTOCOL_VERSION,
                     "X-OEA-Consumer-ID": self.consumer_id},
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=16))
        try:
            response = await self.client.get("/api/protocol", timeout=10)
            response.raise_for_status()
            require(response.json().get("protocol_version") == PROTOCOL_VERSION, "PROTOCOL_MISMATCH", "This client requires an OEA v2 host; migrate and connect explicitly")
        except BaseException:
            await self.client.aclose()
            self.client = None
            raise
        self.opened = True
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def call(self, operation, params=None, *, compact=False, consumer_id=None, agent_name=None):
        require(self.opened and self.loop is asyncio.get_running_loop(), "CLIENT_CLOSED", "Use this open client on its original event loop")
        params = params if params is not None else {}
        timeout = max(30, float(params.get("timeout", 0)) + 5) if isinstance(params.get("timeout", 0), (int, float)) else 30
        response = await self.client.post("/api/rpc", json={"protocol_version": PROTOCOL_VERSION, "operation": operation,
                                                          "params": params, "request_id": new_id("request"), "compact": compact, **({"agent_name": agent_name or self.agent_name} if agent_name or self.agent_name else {})},
            headers={"X-OEA-Consumer-ID": consumer_id} if consumer_id is not None else None, timeout=timeout)
        response.raise_for_status()
        value = response.json()
        require(value.get("protocol_version") == PROTOCOL_VERSION, "PROTOCOL_MISMATCH", "Host response uses another protocol")
        if value.get("error"):
            raise RuntimeFault(**value["error"])
        require("result" in value, "PROTOCOL_ERROR", "Host response has no operation result")
        return value["result"]

    async def events(self, run_id, *, after=0):
        require(self.opened and self.loop is asyncio.get_running_loop(), "CLIENT_CLOSED", "Use this open client on its original event loop")
        async with self.client.stream("GET", "/api/events", params={"run_id": run_id, "after": after}, timeout=None) as response:
            response.raise_for_status()
            async for data in sse_data(response):
                yield json.loads(data)

    async def upload_artifact(self, source, *, project_id, idempotency_key, session_id=None, name=None):
        """Stream an explicitly selected local file through its durable receipt."""
        from oea.artifacts.service import file_io
        require(self.opened and self.loop is asyncio.get_running_loop(), "CLIENT_CLOSED", "Use this open client on its original event loop")
        path = Path(source)
        def describe():
            require(path.is_file(), "NOT_FOUND", "Upload file is unavailable")
            size = path.stat().st_size
            require(size <= 50 * 1024 * 1024, "ARTIFACT_TOO_LARGE", "Upload exceeds 50 MiB")
            with path.open("rb") as original:
                return size, hashlib.file_digest(original, "sha256").hexdigest()
        size, digest = await file_io(describe)
        receipt = await self.call("artifacts.begin_upload", {"project_id": project_id, "session_id": session_id,
            "name": name or path.name, "bytes": size, "sha256": digest, "idempotency_key": idempotency_key})
        if receipt["state"] != "completed":
            async def chunks():
                with path.open("rb") as original:
                    while chunk := await file_io(original.read, 1024 * 1024):
                        yield chunk
            response = await self.client.put("/api/artifact-uploads/" + receipt["id"] + "/content", params={"project_id": project_id},
                content=chunks(), headers={"Content-Type": "application/octet-stream"}, timeout=None)
            value = response.json()
            if value.get("error"):
                raise RuntimeFault(**value["error"])
            response.raise_for_status()
        return await self.call("artifacts.get", {"artifact_id": receipt["id"]})

    async def aclose(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self.opened = False


__all__ = ["PlatformClient", "RuntimeFault"]
