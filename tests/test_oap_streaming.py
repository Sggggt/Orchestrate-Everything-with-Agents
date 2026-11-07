"""OAP file boundaries use real HTTP and bounded streaming buffers."""

import asyncio
import hashlib
from pathlib import Path
import socket
import tempfile
import unittest

import uvicorn

from oea import AgentDefinition, ApplicationBinding, ApplicationClient
from oea.contracts.applications import OAP_PREFIX
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one
from oea.web.app import create_app


class OAPStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oap-stream-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host = await Host(self.root / "state").__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.projects.create("Example", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        registration = await self.host.applications.create("Example")
        self.spec = ApplicationBinding(app_id=registration["application"]["id"], project_id="p", agent_id="worker")
        self.binding = await self.host.applications.bind(self.spec)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(create_app(host=self.host), log_level="warning", access_log=False,
                                             proxy_headers=False, timeout_graceful_shutdown=2))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        async def stop():
            server.should_exit = True
            await serving
        self.addAsyncCleanup(stop)
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.01)
        self.client = await ApplicationClient(origin, registration["credential"]).__aenter__()
        self.addAsyncCleanup(self.client.aclose)
        self.conversation = await self.client.create_conversation(self.binding["id"], idempotency_key="conversation")

    async def test_exact_file_limit_streams_and_one_extra_byte_never_publishes(self):
        block, count = b"generated input\n".ljust(65536, b"."), 800
        size = len(block) * count
        self.assertEqual(size, 50 * 1024 * 1024)
        digest = hashlib.sha256()
        for _ in range(count):
            digest.update(block)
        expected = digest.hexdigest()
        client = self.client
        with self.assertRaises(RuntimeFault) as too_large:
            await client.begin_upload(self.conversation["id"], "oversized.bin", size + 1, expected, idempotency_key="oversized")
        self.assertEqual(too_large.exception.code, "INVALID_REQUEST")
        async def chunks(extra=False):
            for _ in range(count):
                yield block
            if extra:
                yield b"!"
        upload = await client.begin_upload(self.conversation["id"], "maximum.bin", size, expected, idempotency_key="maximum")
        self.assertEqual((await client.upload_content(upload["id"], chunks()))["state"], "completed")
        path = await client.download(upload["id"], self.root / "download.bin")
        with path.open("rb") as stream:
            self.assertEqual(hashlib.file_digest(stream, "sha256").hexdigest(), expected)
        self.assertEqual(path.stat().st_size, size)
        partial = await client.client.get(OAP_PREFIX + "/artifacts/" + upload["id"] + "/content", headers={"Range": "bytes=-17"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, block[-17:])
        overflow = await client.begin_upload(self.conversation["id"], "overflow.bin", size, expected, idempotency_key="overflow")
        with self.assertRaises(RuntimeFault) as invalid:
            await client.upload_content(overflow["id"], chunks(extra=True))
        self.assertEqual(invalid.exception.code, "UPLOAD_SIZE_MISMATCH")
        self.assertEqual((await client.upload_status(overflow["id"]))["state"], "accepted")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM artifacts")["n"]), 1)
        self.assertEqual([item.name for item in (self.host.root / "artifacts").iterdir()], [upload["id"]])

    async def test_revocation_during_upload_prevents_publication_and_preserves_received_bytes(self):
        data = b"original received bytes"
        digest = hashlib.sha256(data).hexdigest()
        upload = await self.client.begin_upload(self.conversation["id"], "input.bin", len(data), digest, idempotency_key="upload")
        entered, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            yield data[:5]
            entered.set()
            await release.wait()
            yield data[5:]
        transfer = asyncio.create_task(self.client.upload_content(upload["id"], chunks()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with asyncio.timeout(5):
                while not self.host.artifacts.receiving:
                    await asyncio.sleep(.01)
            await self.host.applications.bind(self.spec.model_copy(update={"enabled": False}), binding_id=self.binding["id"], expected_revision=1)
            release.set()
            with self.assertRaises(RuntimeFault) as denied:
                await transfer
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
            self.assertEqual((await self.client.upload_status(upload["id"]))["state"], "accepted")
            response = await self.client.client.get(OAP_PREFIX + "/artifacts/" + upload["id"] + "/content")
            self.assertEqual(response.status_code, 403)
            await self.host.applications.bind(self.spec, binding_id=self.binding["id"], expected_revision=2)
            committed = await self.client.commit_upload(upload["id"], idempotency_key="commit")
            self.assertEqual(committed["artifact_id"], upload["id"])
            path = await self.client.download(upload["id"], self.root / "retained.bin")
            self.assertEqual(path.read_bytes(), data)
            count = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE kind='artifact.published'")["n"])
            self.assertEqual(count, 1)
        finally:
            release.set()
            if not transfer.done():
                transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
