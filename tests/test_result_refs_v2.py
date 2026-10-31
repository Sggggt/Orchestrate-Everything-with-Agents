"""Stable delivery across changing domain state, restart and Unicode pages."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import encode, one


class ResultReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-results-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state").__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        session = await self.host.sessions.create("p")
        instance = await self.host.instances.create(session["id"], "worker")
        with patch.object(self.host, "launch"):
            run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Read evidence", idempotency_key="one"))
        self.run_id = run.id

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def issue(self):
        original = await self.host.runs.inspect(self.run_id)
        reference = await self.host.results.issue("runs.inspect", {"run_id": self.run_id}, original, "reader", characters=len(encode(original)))
        return original, reference

    async def read(self, reference, **params):
        return await self.host.results.read(reference["result_ref"], project_id="p", consumer_id="reader", **params)

    async def test_live_state_changes_never_mix_pages_or_repeat_domain_reads(self):
        original, reference = await self.issue()
        first = await self.read(reference, limit=31)
        await self.host.store.call(lambda db: db.execute("UPDATE runs SET error_json=?,state='failed' WHERE id=?",
            (encode({"code": "CHANGED", "safe_message": "Changed"}), self.run_id)), transaction=True)
        async def forbidden(**params):
            self.fail("A snapshot page repeated the domain query")
        self.host.registry.handlers["runs.inspect"] = forbidden
        pieces = [first["text"]]
        page = first
        while page["next_offset"] is not None:
            page = await self.read(reference, offset=page["next_offset"], limit=31)
            self.assertEqual(page["content_version"], first["content_version"])
            self.assertEqual(page["content_sha256"], first["content_sha256"])
            pieces.append(page["text"])
        self.assertEqual(json.loads("".join(pieces)), original)
        self.assertEqual(await self.read(reference, limit=31), first)

    async def test_restart_preserves_snapshot_and_new_query_gets_new_version(self):
        original, reference = await self.issue()
        await self.host.store.call(lambda db: db.execute("UPDATE runs SET state='failed',error_json=? WHERE id=?",
            (encode({"code": "CHANGED", "safe_message": "Changed"}), self.run_id)), transaction=True)
        await self.host.aclose()
        self.host = await Host(self.root / "state").__aenter__()
        self.assertEqual((await self.read(reference, json_pointer="/state"))["value"], original["state"])
        latest, new_reference = await self.issue()
        self.assertEqual(latest["state"], "failed")
        self.assertNotEqual(reference["content_version"], new_reference["content_version"])

    async def test_unicode_pointer_and_exact_end_offsets(self):
        value = {"id": self.run_id, "text": "中文𐐀🙂" * 700}
        reference = await self.host.results.issue("runs.get", {"run_id": self.run_id}, value, "reader", characters=0)
        expected = encode(value["text"])
        parts, offset = [], 0
        while True:
            page = await self.read(reference, json_pointer="/text", offset=offset, limit=7)
            parts.append(page["text"])
            self.assertEqual(page["characters"], len(expected))
            self.assertEqual(page["content_sha256"], hashlib.sha256(expected.encode()).hexdigest())
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual("".join(parts), expected)
        end = await self.read(reference, json_pointer="/text", offset=len(expected))
        self.assertEqual(end["text"], "")
        self.assertTrue(end["complete"])
        with self.assertRaises(RuntimeFault) as caught:
            await self.read(reference, json_pointer="/text", offset=len(expected) + 1)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    async def test_public_schema_and_call_boundaries_match(self):
        schema = self.host.registry.describe("results.read")["parameters_schema"]["properties"]
        self.assertEqual((schema["limit"]["minimum"], schema["limit"]["maximum"]), (1, 2048))
        self.assertEqual(schema["json_pointer"]["maxLength"], 512)
        _, reference = await self.issue()
        params = {"result_ref": reference["result_ref"], "project_id": "p"}
        for invalid in ({"limit": 0}, {"limit": 2049}, {"offset": -1}, {"offset": 2**63}, {"json_pointer": "/" * 513}):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeFault) as caught:
                await self.host.registry.call("results.read", {**params, **invalid}, consumer_id="reader")
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")
            self.assertEqual(caught.exception.details["errors"][0]["path"], [next(iter(invalid))])
        await self.host.registry.call("results.read", {**params, "limit": 2048}, consumer_id="reader")

    async def test_ownership_secret_changes_and_project_cleanup(self):
        value = {"id": self.run_id, "text": "newly-recognized-secret"}
        reference = await self.host.results.issue("runs.get", {"run_id": self.run_id}, value, "reader", characters=0)
        await self.read(reference)
        for project, consumer in (("p", "other"), ("other", "reader")):
            with self.assertRaises(RuntimeFault):
                await self.host.results.read(reference["result_ref"], project_id=project, consumer_id=consumer)
        self.host.vault.known_secrets.add("newly-recognized-secret")
        with self.assertRaises(RuntimeFault) as caught:
            await self.read(reference)
        self.assertEqual(caught.exception.code, "SOURCE_VERSION_UNAVAILABLE")
        await self.host.projects.delete("p", "Project", "delete")
        with self.assertRaises(RuntimeFault):
            await self.read(reference)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM result_snapshots")["n"]), 0)

    async def test_old_unversioned_dynamic_reference_fails_without_query(self):
        _, reference = await self.issue()
        await self.host.store.call(lambda db: db.execute("UPDATE result_refs SET version_json='{}' WHERE id=?", (reference["result_ref"],)), transaction=True)
        with self.assertRaises(RuntimeFault) as caught:
            await self.read(reference)
        self.assertEqual(caught.exception.code, "SOURCE_VERSION_UNAVAILABLE")
