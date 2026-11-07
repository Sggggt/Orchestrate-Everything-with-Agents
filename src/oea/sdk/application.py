"""OAP client for application backends; no platform management passthrough."""

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from oea.contracts.applications import OAP_PREFIX, OAP_VERSION
from oea.contracts.errors import RuntimeFault, require
from oea.models.api import sse_data
from oea.storage.persistence import new_id


def segment(value):
    return quote(value, safe="")


class ApplicationClient:
    def __init__(self, url, credential, *, transport=None):
        parsed = urlsplit(url)
        require(parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.path in {"", "/"} and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                "INVALID_HOST_URL", "Connect to an explicit local host origin")
        self.url, self.credential, self.transport = url.rstrip("/"), credential, transport
        self.client = self.loop = None

    async def __aenter__(self):
        require(self.client is None, "CLIENT_ALREADY_OPEN", "Client is already open")
        self.loop = asyncio.get_running_loop()
        self.client = httpx.AsyncClient(base_url=self.url, transport=self.transport, follow_redirects=False, trust_env=False,
            headers={"Authorization": "Bearer " + self.credential, "X-OAP-Version": OAP_VERSION},
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=16))
        try:
            await self.capabilities()
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        if self.client is not None:
            client, self.client = self.client, None
            await client.aclose()

    def _open(self):
        require(self.client is not None and self.loop is asyncio.get_running_loop(), "CLIENT_CLOSED", "Use this open client on its original event loop")

    @staticmethod
    def _result(response):
        value = response.json()
        require(value.get("protocol_version") == OAP_VERSION, "PROTOCOL_MISMATCH", "Host response uses another OAP version")
        if value.get("error"):
            raise RuntimeFault(**value["error"])
        response.raise_for_status()
        return value["result"]

    async def _request(self, method, path, *, data=None, key=None, params=None):
        self._open()
        headers = {"X-Request-ID": new_id("request")}
        if key is not None:
            headers["Idempotency-Key"] = key
        response = await self.client.request(method, OAP_PREFIX + path, json=data, params=params, headers=headers, timeout=30)
        return self._result(response)

    async def capabilities(self):
        return await self._request("GET", "/capabilities")

    async def agents(self, *, cursor=0, limit=25):
        return await self._request("GET", "/agents", params={"cursor": cursor, "limit": limit})

    async def create_conversation(self, binding_id, *, idempotency_key, name="Conversation"):
        return await self._request("POST", "/conversations", data={"binding_id": binding_id, "name": name}, key=idempotency_key)

    async def conversations(self, *, cursor=0, limit=25):
        return await self._request("GET", "/conversations", params={"cursor": cursor, "limit": limit})

    async def conversation(self, conversation_id):
        return await self._request("GET", "/conversations/" + segment(conversation_id))

    async def history(self, conversation_id, *, after=0, limit=25, version=None):
        return await self._request("GET", "/conversations/" + segment(conversation_id) + "/history", params={"after": after, "limit": limit, **({"version": version} if version is not None else {})})

    async def submit(self, conversation_id, input, *, idempotency_key, artifact_ids=(), policy=None):
        data = {"input": input, "artifact_ids": list(artifact_ids)}
        if policy is not None:
            data["policy"] = policy.model_dump(mode="json") if hasattr(policy, "model_dump") else policy
        return await self._request("POST", "/conversations/" + segment(conversation_id) + "/runs", data=data, key=idempotency_key)

    async def run(self, run_id):
        return await self._request("GET", "/runs/" + segment(run_id))

    async def result(self, run_id, *, offset=0, limit=None):
        return await self._request("GET", "/runs/" + segment(run_id) + "/result", params={"offset": offset, **({"limit": limit} if limit is not None else {})})

    async def response(self, run_id, *, action_id=None, attempt_id=None, version=None, offset=0, limit=8192):
        return await self._request("GET", "/runs/" + segment(run_id) + "/response", params={key: value for key, value in {
            "action_id": action_id, "attempt_id": attempt_id, "version": version, "offset": offset, "limit": limit}.items() if value is not None})

    async def actions(self, run_id, *, cursor=0, limit=25):
        return await self._request("GET", "/runs/" + segment(run_id) + "/actions", params={"cursor": cursor, "limit": limit})

    async def action(self, run_id, action_id, *, json_pointer="", version=None):
        return await self._request("GET", "/runs/" + segment(run_id) + "/actions/" + segment(action_id), params={"json_pointer": json_pointer, **({"version": version} if version is not None else {})})

    async def confirm_action(self, run_id, action_id, *, idempotency_key, evidence, outcome, result=None):
        return await self._request("POST", "/runs/" + segment(run_id) + "/actions/" + segment(action_id) + "/confirm",
                                   data={"evidence": evidence, "outcome": outcome, "result": result}, key=idempotency_key)

    async def resume(self, run_id, *, idempotency_key, input=None, execution_limits=None):
        data = {"input": input}
        if execution_limits is not None:
            data["execution_limits"] = execution_limits.model_dump(mode="json") if hasattr(execution_limits, "model_dump") else execution_limits
        return await self._request("POST", "/runs/" + segment(run_id) + "/resume", data=data, key=idempotency_key)

    async def cancel(self, run_id, *, idempotency_key):
        return await self._request("POST", "/runs/" + segment(run_id) + "/cancel", data={}, key=idempotency_key)

    async def close_conversation(self, conversation_id, *, idempotency_key):
        return await self._request("POST", "/conversations/" + segment(conversation_id) + "/close", data={}, key=idempotency_key)

    async def command(self, run_id, command_id, *, stdout_offset=0, stderr_offset=0):
        return await self._request("GET", "/runs/" + segment(run_id) + "/commands/" + segment(command_id), params={"stdout_offset": stdout_offset, "stderr_offset": stderr_offset})

    async def download_command(self, run_id, command_id, stream, destination, *, offset=0, limit=None):
        """Download retained bytes directly; a snapshot never reruns the command."""
        self._open()
        require(stream in {"stdout", "stderr"}, "INVALID_REQUEST", "Choose stdout or stderr")
        path = "/runs/" + segment(run_id) + "/commands/" + segment(command_id) + "/output/" + stream
        destination = Path(destination)
        async with self.client.stream("GET", OAP_PREFIX + path, params={"offset": offset, **({"limit": limit} if limit is not None else {})}, timeout=120) as response:
            if response.is_error:
                await response.aread()
                self._result(response)
            with destination.open("xb") as output:
                async for packet in response.aiter_bytes():
                    output.write(packet)
        return destination

    async def artifacts(self, run_id, *, cursor=0, limit=25):
        return await self._request("GET", "/runs/" + segment(run_id) + "/artifacts", params={"cursor": cursor, "limit": limit})

    async def candidates(self, run_id, *, cursor=0, limit=25):
        return await self._request("GET", "/runs/" + segment(run_id) + "/candidates", params={"cursor": cursor, "limit": limit})

    async def events(self, run_id, *, after=0):
        self._open()
        async with self.client.stream("GET", OAP_PREFIX + "/runs/" + segment(run_id) + "/events", params={"after": after}, timeout=None) as response:
            if response.is_error:
                await response.aread()
                self._result(response)
            async for data in sse_data(response):
                yield json.loads(data)

    async def begin_upload(self, conversation_id, name, bytes, sha256, *, idempotency_key):
        return await self._request("POST", "/uploads", data={"conversation_id": conversation_id, "name": name, "bytes": bytes, "sha256": sha256}, key=idempotency_key)

    async def upload_status(self, upload_id):
        return await self._request("GET", "/uploads/" + segment(upload_id))

    async def upload_content(self, upload_id, chunks):
        self._open()
        response = await self.client.put(OAP_PREFIX + "/uploads/" + segment(upload_id) + "/content", content=chunks, timeout=120)
        return self._result(response)

    async def commit_upload(self, upload_id, *, idempotency_key):
        return await self._request("POST", "/uploads/" + segment(upload_id) + "/commit", data={}, key=idempotency_key)

    async def download(self, artifact_id, destination):
        """Stream an immutable artifact into a new file and verify its receipt hash."""
        self._open()
        destination = Path(destination)
        digest = hashlib.sha256()
        async with self.client.stream("GET", OAP_PREFIX + "/artifacts/" + segment(artifact_id) + "/content", timeout=120) as response:
            if response.is_error:
                await response.aread()
                self._result(response)
            with destination.open("xb") as output:
                async for packet in response.aiter_bytes():
                    digest.update(packet)
                    output.write(packet)
            require(digest.hexdigest() == response.headers.get("X-Content-SHA256"), "ARTIFACT_CONTENT_CHANGED", "Downloaded bytes differ from the artifact receipt")
        return destination
