"""Workflow execution scopes, legacy migration and strict snapshot compatibility."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apsw

from oea import AgentConfig, AgentDescriptor, Runtime, RuntimeConfig, RuntimeFault, TaskSpec, WorkflowBuilder
from ma_runtime.bridges.compact import CompactBridge
from ma_runtime.bridges.dispatch import dispatch
from ma_runtime.models import ThreadSnapshot, json_dump
from ma_runtime.store import StoreActor, one, rows


def graph(*, waiting=False, mapped=False):
    builder = WorkflowBuilder().node("start", "start")
    payload = {"input": "read", "_mock": {"delay": 0, "wait_for_input": waiting}}
    if mapped:
        builder.node("worker", "map", agent_ids=["worker"], items=[1, 2], payload=payload)
    else:
        builder.node("worker", "agent", agent_id="worker", payload=payload)
    return (builder.node("end", "end", outputs={"result": "{{nodes.worker}}"})
            .connect("start", "worker").connect("worker", "end").build())


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-ownership-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root / "state", RuntimeConfig(max_concurrency=4, reserved_external_slots=0)).__aenter__()
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock"))
        self.session = await self.rt.sessions.create()

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def wait(self, run_id, state):
        async with asyncio.timeout(5):
            while (run := await self.rt.workflows.get_run(run_id)).state != state:
                await asyncio.sleep(0.025)
            return run

    async def test_shared_session_cancel_completion_and_uncertainty_are_owned(self):
        first = await self.rt.workflows.create("default", "First", "first", graph=graph(waiting=True))
        second = await self.rt.workflows.create("default", "Second", "second", graph=graph(waiting=True))
        runs = [await self.rt.workflows.run(w.id, {}, key, session_id=self.session.id)
                for w, key in ((first, "a"), (first, "b"), (second, "c"))]
        snapshots = [await self.wait(r.id, "WAITING") for r in runs]
        ordinary = await self.rt.threads.create(self.session.id, "worker")
        task = await self.rt.tasks.submit(TaskSpec(session_id=self.session.id, thread_id=ordinary.id,
            agent_id="worker", payload={"input": "ordinary", "_mock": {"delay": 0, "wait_for_input": True}}, idempotency_key="ordinary"))
        async with asyncio.timeout(5):
            while (await self.rt.tasks.get(task.id)).state != "WAITING":
                await asyncio.sleep(0.025)
        self.assertIsNone((await self.rt.threads.ownership(ordinary.id))["workflow_run_id"])
        for handle, snapshot in zip(runs, snapshots):
            node = next(n for n in snapshot.nodes if n.node_id == "worker")
            thread = (await self.rt.tasks.get(node.task_id)).thread_id
            self.assertEqual((await self.rt.threads.ownership(thread))["workflow_run_id"], handle.id)
            self.assertNotIn("workflow_run_id", (await self.rt.threads.get(thread)).model_dump())
        await self.rt.workflows.cancel(runs[0].id)
        await self.wait(runs[0].id, "CANCELLED")
        self.assertEqual([(await self.rt.workflows.get_run(r.id)).state for r in runs[1:]], ["WAITING", "WAITING"])
        self.assertEqual((await self.rt.tasks.get(task.id)).state, "WAITING")
        # Isolated persisted uncertainty must not hold an unrelated run open.
        await self.rt.store.call(lambda db: db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=?", (task.id,)), transaction=True)
        simple = await self.rt.workflows.create("default", "Deterministic", "simple")
        done = await self.rt.workflows.run(simple.id, {"value": 3}, "done", session_id=self.session.id)
        self.assertEqual((await self.rt.workflows.result(done.id, timeout=3))["outputs"], {"result": {"value": 3}})
        await self.rt.store.call(lambda db: db.execute("UPDATE tasks SET state='CANCELLED' WHERE id=?", (task.id,)), transaction=True)

    async def test_run_receipt_default_and_cross_project_contracts(self):
        saved = await self.rt.workflows.create("default", "Flow", "definition")
        run = await self.rt.workflows.run(saved.id, {"x": 1}, "explicit", session_id=self.session.id)
        again = await self.rt.workflows.run(saved.id, {"x": 1}, "explicit", session_id=self.session.id)
        self.assertEqual(run.id, again.id)
        self.assertTrue(again.reused)
        other_session = await self.rt.sessions.create()
        for options in ({"session_id": other_session.id}, {}, {"session_id": self.session.id, "inputs": {"x": 2}}):
            with self.assertRaises(RuntimeFault) as error:
                await self.rt.workflows.run(saved.id, options.pop("inputs", {"x": 1}), "explicit", **options)
            self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        fresh = await self.rt.workflows.run(saved.id, {}, "implicit")
        self.assertNotEqual(fresh.session_id, self.session.id)
        self.assertEqual((await self.rt.workflows.run(saved.id, {}, "implicit")).id, fresh.id)
        project = await self.rt.projects.create("Other", str(self.root), "other")
        foreign = await self.rt.sessions.create(project_id=project.id)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.run(saved.id, {}, "foreign", session_id=foreign.id)
        self.assertEqual(error.exception.code, "PROJECT_MISMATCH")
        closed = await self.rt.sessions.create(project_id="default")
        await self.rt.sessions.close(closed.id)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.workflows.run(saved.id, {}, "closed", session_id=closed.id)
        self.assertEqual(error.exception.code, "SESSION_CLOSED")
        async def invoke(method, params):
            return json.loads(json_dump(await dispatch(self.rt, method, params)))
        bridge = CompactBridge(invoke)
        self.assertIn("session_id", json.dumps(bridge.guide("workflows", "run")))
        response = await bridge.call("workflows", "run", {"workflow_id": saved.id, "inputs": {},
                                    "idempotency_key": "bridge", "session_id": self.session.id})
        self.assertTrue(response["ok"])

    async def test_map_origin_is_immutable_and_only_owned_tasks_cancel(self):
        saved = await self.rt.workflows.create("default", "Map", "map", graph=graph(waiting=True, mapped=True))
        first = await self.rt.workflows.run(saved.id, {}, "one", session_id=self.session.id)
        second = await self.rt.workflows.run(saved.id, {}, "two", session_id=self.session.id)
        run = await self.wait(first.id, "WAITING")
        await self.wait(second.id, "WAITING")
        node = next(n for n in run.nodes if n.batch_id)
        members = (await self.rt.instances.get(node.batch_id))["instances"]
        for member in members:
            self.assertEqual((await self.rt.threads.ownership(member["thread_id"]))["workflow_run_id"], first.id)
            with self.assertRaises(apsw.ConstraintError):
                await self.rt.store.call(lambda db: db.execute("UPDATE thread_workflow_runs SET run_id=? WHERE thread_id=?",
                                        (second.id, member["thread_id"])), transaction=True)
        await self.rt.workflows.cancel(first.id)
        await self.wait(first.id, "CANCELLED")
        self.assertEqual((await self.rt.workflows.get_run(second.id)).state, "WAITING")
        self.assertEqual((await self.rt.instances.get(node.batch_id))["counts"], {"CANCELLED": 2})


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_owned_tasks_and_cursors_survive_parent_table_rebuild(self):
        from ma_runtime.workflows import default_graph
        with tempfile.TemporaryDirectory(prefix="workflow-migration-") as directory:
            root = Path(directory)
            db = apsw.Connection(str(root / "runtime.sqlite"))
            schemas = Path(__file__).parents[1] / "src/ma_runtime/store"
            for version in range(1, 15):
                source = (schemas / ("schema.sql" if version == 1 else f"schema-{version}.sql")).read_text(encoding="utf-8")
                db.execute(source)
                db.execute("INSERT INTO schema_migrations VALUES(?,?,1)", (version, hashlib.sha256(source.encode()).hexdigest()))
            db.execute("INSERT INTO sessions(id,state,title,metadata_json,created_at,project_id) VALUES('session','OPEN','','{}',1,'default')")
            definition = json_dump(default_graph())
            db.execute("INSERT INTO workflows(id,project_id,name,graph_json,creation_key,request_hash,created_at,updated_at) VALUES('flow','default','Legacy',?,'definition','legacy',1,1)", (definition,))
            db.execute("INSERT INTO workflow_runs(rowid,id,workflow_id,project_id,session_id,revision,graph_json,inputs_json,state,idempotency_key,request_hash,created_at,updated_at,ended_at) VALUES(7,'run','flow','default','session',1,?,'{}','SUCCEEDED','key','legacy',1,1,1)", (definition,))
            for identity in ("node", "member", "child", "ordinary"):
                descriptor = json_dump(AgentDescriptor(agent_id="worker"))
                db.execute("INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,created_at) VALUES(?,'session','worker','',?,1)", (identity, descriptor))
                spec = json_dump(TaskSpec(session_id="session", thread_id=identity, agent_id="worker", payload={}, idempotency_key=identity))
                db.execute("INSERT INTO tasks(id,session_id,thread_id,spec_json,state,idempotency_key,request_hash,created_at,updated_at) VALUES(?,'session',?,?,'SUCCEEDED',?,'legacy',1,1)", (identity, identity, spec, identity))
                db.execute("INSERT INTO runs(id,task_id,attempt_no,host_instance_id,expected_revision,state,adapter_snapshot_json,held_resources_json,started_at,ended_at) VALUES(?,?,1,'legacy',0,'SUCCEEDED',?,'[]',1,2)",
                           ("attempt-" + identity, identity, descriptor))
                for state in ("RUNNING", "SUCCEEDED"):
                    attributes = {"max_output_tokens": 20, "context": {"input_tokens": 12}}
                    if state == "SUCCEEDED" and identity != "child":
                        attributes["usage"] = {"prompt_tokens": 2, "completion_tokens": 3}
                    db.execute("INSERT INTO events(id,session_id,thread_id,task_id,run_id,type,payload_json,created_at) VALUES(?,'session',?,?,?,'run.stage',?,2)",
                               (state + identity, identity, identity, "attempt-" + identity,
                                json_dump({"id": "stage-" + identity, "stage": "model", "state": state, "attributes": attributes})))
            db.execute("INSERT INTO workflow_node_runs(run_id,node_id,type,title,state,task_id) VALUES('run','node','agent','','SUCCEEDED','node')")
            for batch, parent, task in (("map", None, "member"), ("nested", "node", "child")):
                db.execute("INSERT INTO instance_batches(id,session_id,parent_task_id,idempotency_key,request_hash,max_concurrency,routing,created_at) VALUES(?,'session',?,?,'legacy',1,'round_robin',1)", (batch, parent, batch))
                db.execute("INSERT INTO instance_members VALUES(?,0,?)", (batch, task))
            db.execute("INSERT INTO workflow_node_runs(run_id,node_id,type,title,state,batch_id) VALUES('run','map','map','','SUCCEEDED','map')")
            db.close()
            original_read = Path.read_text
            def failing_migration(path, *args, **kwargs):
                source = original_read(path, *args, **kwargs)
                return source + "\nSELECT * FROM missing_migration_table;" if path.name == "schema-15.sql" else source
            store = StoreActor(root)
            try:
                with patch.object(Path, "read_text", failing_migration), self.assertRaises(apsw.SQLError):
                    await store.open()
                self.assertEqual(await store.call(lambda db: one(db, "SELECT MAX(version) AS version FROM schema_migrations").version), 14)
                self.assertEqual(await store.call(lambda db: db.pragma("foreign_keys")), 1)
                self.assertIsNone(await store.call(lambda db: one(db, "SELECT name FROM sqlite_master WHERE name='workflow_tasks'")))
            finally:
                await store.close()
            async with Runtime.open(root) as rt:
                self.assertTrue((root / "backups/before-schema-15.sqlite").is_file())
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT rowid AS position FROM workflow_runs WHERE id='run'").position), 7)
                self.assertEqual(await rt.store.call(lambda db: rows(db, "PRAGMA foreign_key_check")), [])
                self.assertEqual(await rt.store.call(lambda db: db.pragma("foreign_keys")), 1)
                for identity in ("node", "member", "child"):
                    self.assertEqual((await rt.threads.ownership(identity))["workflow_run_id"], "run")
                self.assertIsNone((await rt.threads.ownership("ordinary"))["workflow_run_id"])
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM workflow_tasks").count), 3)
                # Original strict thread model still reads the unextended view.
                ThreadSnapshot.model_validate((await rt.threads.get("node")).model_dump())
                budget = await rt.budgets.for_task("node")
                self.assertEqual(budget["usage"]["model_calls"], 3)
                self.assertEqual(budget["usage"]["input_tokens"], 16)
                self.assertEqual(budget["usage"]["output_tokens"], 26)
                self.assertEqual(budget["estimated_model_calls"], 1)
                self.assertEqual((await rt.budgets.for_task("child"))["id"], budget["id"])
                self.assertNotEqual((await rt.budgets.for_task("ordinary"))["id"], budget["id"])
            async with Runtime.open(root) as rt:
                self.assertEqual((await rt.budgets.for_task("node"))["usage"], budget["usage"])
