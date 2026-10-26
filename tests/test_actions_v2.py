"""Real tool receipts survive response loss without repeating side effects."""

import asyncio
import tempfile
import unittest
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from oea.tools.gateway import ToolGateway
from oea.contracts.errors import RuntimeFault, require
from oea.tools.functions import tool
from oea.storage.persistence import Store, rows
from oea.tools.contracts import ToolSpec


@dataclass
class Submitted:
    count: int


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-v2-actions-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        await self.store.open()
        self.allowed = True

        def seed(db):
            db.execute("INSERT INTO projects VALUES('p','Project',NULL,0,1)")
            db.execute("INSERT INTO agent_definitions VALUES('worker','p','{}',1,1)")
            db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('s','p','Session',1)")
            db.execute("INSERT INTO budgets(id,project_id,root_run_id,policy_json,started_at,created_at) VALUES('b','p','r','{}',1,1)")
            for instance, run in (("i", "r"), ("j", "other")):
                db.execute("INSERT INTO instances VALUES(?,'p','s','worker',?,'main','{}','{}',?,1)", (instance, instance, run))
                db.execute("INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at) VALUES(?,?,?,'b','running','{}','key','hash','main',1,1)", (run, instance, run))
        await self.store.call(seed, transaction=True)
        self.gateway = ToolGateway(self.store, authorize=self.authorize)

    async def authorize(self, run_id, name):
        require(self.allowed, "PERMISSION_DENIED", "Tool binding was revoked")

    async def asyncTearDown(self):
        await self.gateway.aclose()
        await self.store.close()
        self.temporary.cleanup()

    async def test_dataclass_result_and_same_key_replay_submit_only_once_after_reopen(self):
        submissions = []

        @tool(side_effects="local")
        def submit(count: int) -> Submitted:
            submissions.append(count)
            return Submitted(count)

        self.gateway.register(submit)
        self.assertEqual(await self.gateway.invoke("r", "submit", {"count": 3}, "submit-1"), {"count": 3})
        await self.gateway.aclose()
        await self.store.close()
        self.store = Store(self.root)
        await self.store.open()
        self.gateway = ToolGateway(self.store, authorize=self.authorize)
        self.gateway.register(submit)
        self.assertEqual(await self.gateway.invoke("r", "submit", {"count": 3}, "submit-1"), {"count": 3})
        self.assertEqual(submissions, [3])

    async def test_concurrent_replay_joins_original_action(self):
        started, release = asyncio.Event(), asyncio.Event()
        submissions = []

        @tool(side_effects="local")
        async def submit(count: int) -> Submitted:
            submissions.append(count)
            started.set()
            await release.wait()
            return Submitted(count)

        self.gateway.register(submit)
        requests = [asyncio.create_task(self.gateway.invoke("r", "submit", {"count": 3}, "same-key")) for _ in range(20)]
        await asyncio.wait_for(started.wait(), 2)
        release.set()
        self.assertEqual(await asyncio.gather(*requests), [{"count": 3}] * 20)
        self.assertEqual(submissions, [3])

    async def test_unknown_side_effect_is_not_replayed_and_does_not_block_another_run(self):
        submissions = []

        @tool(side_effects="external")
        async def submit(count: int) -> Submitted:
            submissions.append(count)
            raise RuntimeError("Response lost after external submission")

        @tool
        async def read(count: int) -> int:
            return count

        self.gateway.register(submit)
        self.gateway.register(read)
        for _ in range(2):
            with self.assertRaises(RuntimeFault) as caught:
                await self.gateway.invoke("r", "submit", {"count": 3}, "submission")
            self.assertEqual(caught.exception.code, "OUTCOME_UNCERTAIN")
        self.assertEqual(await self.gateway.invoke("other", "read", {"count": 4}, "read"), {"value": 4})
        self.assertEqual(submissions, [3])

    async def test_revoked_binding_and_invalid_parameters_do_not_execute(self):
        submissions = []

        @tool(side_effects="local")
        async def submit(count: int) -> Submitted:
            submissions.append(count)
            return Submitted(count)

        self.gateway.register(submit)
        with self.assertRaises(RuntimeFault):
            await self.gateway.invoke("r", "submit", {"count": "invalid"}, "bad")
        self.gateway.catalog(["submit"])
        self.allowed = False
        with self.assertRaises(RuntimeFault) as caught:
            await self.gateway.invoke("r", "submit", {"count": 3}, "revoked")
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")
        self.assertEqual(submissions, [])
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT * FROM actions")), [])

    async def test_input_snapshot_matches_receipt_when_caller_mutates_its_dictionary(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Provider:
            def describe(self):
                return ToolSpec(name="snapshot", input_schema={"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}, output_schema={"type": "object"})
            async def invoke(self, request):
                started.set()
                await release.wait()
                return {"value": request["arguments"]["count"]}
        self.gateway.register(Provider())
        arguments = {"count": 3}
        pending = asyncio.create_task(self.gateway.invoke("r", "snapshot", arguments, "original"))
        await asyncio.wait_for(started.wait(), 2)
        arguments["count"] = 99
        release.set()
        self.assertEqual(await pending, {"value": 3})

    async def test_revocation_after_intent_but_before_dispatch_is_a_known_failure(self):
        checked = 0
        async def authorize(run_id, name):
            nonlocal checked
            checked += 1
            require(checked == 1, "PERMISSION_DENIED", "Grant revoked before dispatch")
        self.gateway.authorize = authorize
        called = []
        @tool(side_effects="external")
        async def submit() -> int:
            called.append(1)
            return 37
        self.gateway.register(submit)
        with self.assertRaises(RuntimeFault) as denied:
            await self.gateway.invoke("r", "submit", {}, "revoked-before-dispatch")
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        action = (await self.store.call(lambda db: rows(db, "SELECT state FROM actions")))[0]
        self.assertEqual(action["state"], "failed")
        self.assertEqual(called, [])

    async def test_typed_date_is_validated_before_accepting_a_side_effect(self):
        called = []
        @tool(side_effects="external")
        def submit(when: date) -> str:
            called.append(when)
            return when.isoformat()
        self.gateway.register(submit)
        with self.assertRaises(RuntimeFault) as invalid:
            await self.gateway.invoke("r", "submit", {"when": "2026-99-40"}, "date-call")
        self.assertEqual(invalid.exception.code, "INVALID_REQUEST")
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT id FROM actions")), [])
        self.assertEqual(called, [])
        result = await self.gateway.invoke("r", "submit", {"when": "2026-10-25"}, "date-call")
        self.assertEqual(result, {"value": "2026-10-25"})
        self.assertEqual(called, [date(2026, 10, 25)])

    async def test_schema_references_are_bundled_without_implicit_http(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requests.append(self.path)
                body = json.dumps({"type": "object"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        class Provider:
            def __init__(self, schema):
                self.schema = schema
            def describe(self):
                return ToolSpec(name="schema_input", input_schema=self.schema, output_schema={"type": "object"})
            async def invoke(self, request):
                return {"value": request["arguments"]["value"]}
        try:
            with self.assertRaises(RuntimeFault) as unresolved:
                self.gateway.register(Provider({"$ref": "http://127.0.0.1:" + str(server.server_port) + "/schema"}))
            self.assertEqual(unresolved.exception.code, "SCHEMA_REFERENCE_UNAVAILABLE")
            with self.assertRaises(RuntimeFault) as nested:
                self.gateway.register(Provider({"x-schemas": {"Nested": {"$ref": "http://127.0.0.1:" + str(server.server_port) + "/nested"}}, "$ref": "#/x-schemas/Nested"}))
            self.assertEqual(nested.exception.code, "SCHEMA_REFERENCE_UNAVAILABLE")
            local = {"$id": "https://inline.example/root", "$defs": {"Input": {"$id": "arguments", "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}}, "$ref": "arguments"}
            self.gateway.register(Provider(local))
            self.assertEqual(await self.gateway.invoke("r", "schema_input", {"value": 37}, "local-reference"), {"value": 37})
            self.assertEqual(requests, [])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
