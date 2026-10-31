"""Permanent project deletion uses generated external state and preserves other owners."""

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from oea.contracts.configuration import AgentDefinition
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.mcp.upstream import MCPServer
from oea.skills.service import SkillSource
from oea.storage.persistence import one, rows
from oea.tools.contracts import ToolSpec


class ProjectDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = self.root / "original-project"
        self.original.mkdir()
        (self.original / "source.txt").write_text("Original project input", encoding="utf-8")
        self.host = Host(self.root / "state")
        await self.host.__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.projects.create("Example", project_id="p", folder=self.original)
        await self.host.projects.create("Other", project_id="other")
        await self.host.agents.save(AgentDefinition(id="global", name="Global"))
        for project in ("p", "other"):
            await self.host.agents.save(AgentDefinition(id=project + "-agent", name="Example", project_id=project))
            await self.host.sessions.create(project, session_id=project + "-session")
            await self.host.instances.create(project + "-session", project + "-agent", name="worker")
        self.instances = await self.host.store.call(lambda db: {row["project_id"]: row["id"] for row in rows(db, "SELECT id,project_id FROM instances")})
        self.artifact_files = []
        for project in ("p", "other"):
            artifact = self.root / (project + "-artifact.txt")
            artifact.write_text("Generated artifact " + project, encoding="utf-8")
            self.artifact_files.append(artifact)
            await self.host.store.call(lambda db, project=project, artifact=artifact: self.populate(db, project, artifact), transaction=True)
        await self.host.mcp.open()
        await self.host.skills.open()

    def populate(self, db, project, artifact):
        p = {"project": project, "instance": self.instances[project], "session": project + "-session", "artifact_path": str(artifact)}
        for field in ("budget", "run", "workflow", "workflow_run", "message", "resumption", "action", "intent", "control", "usage", "source", "memory", "artifact", "upload", "step", "workflow_resumption", "server", "skill", "private_body", "private_contract"):
            p[field] = project + "-" + field
        p["server_json"] = MCPServer(id=p["server"], project_id=project, command=("example-server",)).model_dump_json()
        p["skill_json"] = SkillSource(id=p["skill"], project_id=project, path=self.root / (project + "-skill-source")).model_dump_json()
        statements = (
            "INSERT INTO budgets(id,project_id,session_id,policy_json,created_at) VALUES(:budget,:project,:session,'{}',1)",
            "INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at) VALUES(:run,:instance,:run,:budget,'completed','{}','run-key','hash','main',1,1)",
            "INSERT INTO workflows(id,project_id,name,revision,graph_json,created_at,updated_at) VALUES(:workflow,:project,'Example',1,'{}',1,1)",
            "INSERT INTO workflow_runs(id,workflow_id,session_id,budget_id,root_workflow_run_id,revision,graph_json,inputs_json,state,idempotency_key,input_hash,created_at,updated_at) VALUES(:workflow_run,:workflow,:session,:budget,:workflow_run,1,'{}','{}','completed','workflow-key','hash',1,1)",
            "INSERT INTO messages(id,project_id,session_id,source_json,purpose,body,root_run_id,idempotency_key,input_hash,created_at) VALUES(:message,:project,:session,'{}','inform','Generated message',:run,'message-key','hash',1)",
            "UPDATE runs SET source_message_id=:message WHERE id=:run",
            "INSERT INTO run_resumptions(id,run_id,idempotency_key,input_hash,input_json,created_at,event_position,source_message_id) VALUES(:resumption,:run,'resume-key','hash','{}',1,1,:message)",
            "INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,created_at,updated_at) VALUES(:action,:run,'action-key','model.complete','hash','{}','none','completed',1,1)",
            "INSERT INTO tool_intents(id,project_id,consumer_id,name,binding_json,idempotency_key,input_hash,input_json,created_at) VALUES(:intent,:project,'controller','example','{}','intent-key','hash','{}',1)",
            "INSERT INTO actions(id,intent_id,operation_key,name,input_hash,side_effects,state,created_at,updated_at) VALUES(:control,:intent,'control-key','example','hash','external','completed',1,1)",
            "INSERT OR IGNORE INTO tool_contracts VALUES('shared-contract','{}')",
            "INSERT INTO tool_contracts VALUES(:private_contract,'{}')",
            "INSERT INTO action_contracts VALUES(:action,'shared-contract')",
            "INSERT INTO action_contracts VALUES(:control,:private_contract)",
            "INSERT INTO action_resolutions(action_id,idempotency_key,input_hash,evidence,outcome,created_at) VALUES(:control,'resolution-key','hash','Confirmed','completed',1)",
            "INSERT INTO usage(id,budget_id,run_id,action_id,purpose,kind,state,input_tokens,output_tokens,created_at) VALUES(:usage,:budget,:run,:action,'main','model','settled',1,2,1)",
            "INSERT INTO model_failures(id,action_id,input_hash,input_json,error_json,created_at) VALUES(:usage,:action,'hash','{}','{}',1)",
            "INSERT INTO context_sources(id,project_id,instance_id,run_id,origin,sha256,body,created_at) VALUES(:source,:project,:instance,:run,'fixture','hash','Original evidence',1)",
            "INSERT INTO context_grants VALUES(:source,:instance,:message)",
            "INSERT INTO message_context_refs VALUES(:message,:source)",
            "INSERT INTO conversation(id,instance_id,run_id,role,resumption_id,created_at) VALUES(:resumption,:instance,:run,'user',:resumption,1)",
            "INSERT INTO memories(id,instance_id,run_id,through_position,source_ids_json,body,created_at) VALUES(:memory,:instance,:run,1,'[]','Remembered evidence',1)",
            "INSERT INTO artifacts VALUES(:artifact,:project,:instance,:run,'result.txt',:artifact_path,'hash',1,'text/plain',1,:session,'run')",
            "INSERT INTO artifacts VALUES(:upload,:project,NULL,NULL,'upload.txt',:artifact_path,'hash',1,'text/plain',1,:session,'upload')",
            "INSERT INTO artifact_uploads VALUES(:upload,:project,:session,'controller','upload-key','hash','upload.txt',1,'hash','text/plain',1)",
            "INSERT INTO run_artifacts VALUES(:run,:artifact)",
            "INSERT INTO deliveries(message_id,instance_id,run_id,accepted_at) VALUES(:message,:instance,:run,1)",
            "INSERT INTO read_positions VALUES('controller',:session,1,1)",
            "INSERT INTO workflow_resumptions(id,workflow_run_id,idempotency_key,input_json,input_hash,created_at) VALUES(:workflow_resumption,:workflow_run,'workflow-resume','{}','hash',1)",
            "INSERT INTO workflow_steps(id,workflow_run_id,node_id,operation_key,input_hash,checkpoint_id,native_step,state,repair_request_id,created_at,updated_at) VALUES(:step,:workflow_run,'node','step-key','hash','checkpoint',1,'completed',:workflow_resumption,1,1)",
            "INSERT INTO workflow_nodes VALUES(:workflow_run,'node','completed',:step,NULL,1)",
            "INSERT INTO workflow_step_failures VALUES(:step,'failed-hash','failed',NULL,'{}',1)",
            "INSERT INTO workflow_map_members VALUES(:step,0,:instance,:run,NULL)",
            "INSERT INTO run_progress VALUES(:run,'progress','fixture',1)",
            "INSERT INTO run_activity(run_id,stalled_model_calls) VALUES(:run,0)",
            "INSERT INTO mcp_servers VALUES(:server,:project,:server_json,1,1)",
            "INSERT INTO mcp_catalogs VALUES(:server,1,'[]',1)",
            "INSERT INTO run_mcp_bindings VALUES(:run,:server,1)",
            "INSERT INTO skills VALUES(:skill,:project,:skill_json,1,0,1)",
            "INSERT INTO skill_bodies VALUES(:private_body,'Private generated skill')",
            "INSERT OR IGNORE INTO skill_bodies VALUES('shared-body','Shared generated skill')",
            "INSERT INTO skill_versions VALUES(:skill,1,'/example',:private_body,'Example','',1)",
            "INSERT INTO skill_versions VALUES(:skill,2,'/example','shared-body','Example','',1)",
            "INSERT INTO skill_loads VALUES(:run,:skill,'SKILL.md',:source,'SKILL.md')",
            "INSERT OR IGNORE INTO imported_migrations VALUES('migration','hash',29,1,'{}')",
            "INSERT INTO imported_history(migration_id,table_name,row_key,row_json,sha256,project_id,session_id,instance_id,review_state,created_at) VALUES('migration','conversation',:project,json_object('id',:project,'content','Archived'), 'hash',:project,:session,:instance,'needs_review',1)",
            "INSERT INTO imported_transcript VALUES(last_insert_rowid(),:session,:instance,1,'assistant',1,1)",
            "INSERT INTO imported_links VALUES('migration','conversation',:project,'message',:message)",
            "INSERT INTO result_refs VALUES(:project,'controller',:project,'runs.get','{}','{}','{}',1)",
            "INSERT INTO events(kind,project_id,session_id,instance_id,run_id,data_json,created_at) VALUES('fixture',:project,:session,:instance,:run,'{}',1)",
        )
        for statement in statements:
            db.execute(statement, p)

    async def delete(self, **options):
        return await self.host.projects.delete("p", options.get("confirm_name", "Example"), options.get("idempotency_key", "delete-key"))

    async def test_schema_used_only_by_another_projects_archived_attempt_is_preserved(self):
        def seed(db):
            db.execute("INSERT INTO tool_contracts VALUES('archived-contract','{}')")
            db.execute("UPDATE actions SET input_json=json_object('projection',json_object('tool_contracts',json_object('example','archived-contract'))) WHERE id='p-action'")
            db.execute("UPDATE model_failures SET input_json=json_object('projection',json_object('tool_contracts',json_object('example','archived-contract'))) WHERE id='other-usage'")
        await self.host.store.call(seed, transaction=True)
        await self.delete()
        self.assertIsNotNone(await self.host.store.call(lambda db: one(db, "SELECT * FROM tool_contracts WHERE id='archived-contract'")))
        await self.host.projects.delete("other", "Other", "delete-other")
        self.assertIsNone(await self.host.store.call(lambda db: one(db, "SELECT * FROM tool_contracts WHERE id='archived-contract'")))

    async def test_deletes_complete_aggregate_and_preserves_other_project_global_and_files(self):
        signal = self.host.run_signal("p-run")
        before_other = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM runs WHERE id='other-run'")["n"])
        receipt = await self.delete()
        self.assertTrue(receipt["deleted"])
        self.assertTrue(signal.is_set())
        self.assertGreater(receipt["records_deleted"], 20)
        def check(db):
            self.assertIsNone(one(db, "SELECT * FROM projects WHERE id='p'"))
            self.assertIsNotNone(one(db, "SELECT * FROM projects WHERE id='other'"))
            self.assertIsNotNone(one(db, "SELECT * FROM agent_definitions WHERE id='global'"))
            self.assertIsNone(one(db, "SELECT * FROM agent_definitions WHERE id='p-agent'"))
            self.assertEqual(one(db, "SELECT COUNT(*) AS n FROM runs WHERE id='other-run'")["n"], before_other)
            self.assertIsNone(one(db, "SELECT * FROM tool_contracts WHERE id='p-private_contract'"))
            self.assertIsNotNone(one(db, "SELECT * FROM tool_contracts WHERE id='shared-contract'"))
            self.assertIsNone(one(db, "SELECT * FROM skill_bodies WHERE sha256='p-private_body'"))
            self.assertIsNotNone(one(db, "SELECT * FROM skill_bodies WHERE sha256='shared-body'"))
            self.assertEqual(one(db, "SELECT COUNT(*) AS n FROM imported_history WHERE project_id='p'")["n"], 0)
            self.assertEqual(one(db, "SELECT COUNT(*) AS n FROM imported_links WHERE source_id='p'")["n"], 0)
            self.assertIsNotNone(one(db, "SELECT * FROM imported_migrations WHERE id='migration'"))
            self.assertEqual([row["kind"] for row in rows(db, "SELECT kind FROM events WHERE project_id='p'")], ["project.deleted"])
            self.assertEqual(rows(db, "PRAGMA foreign_key_check"), [])
            self.assertEqual(db.pragma("foreign_keys"), 1)
        await self.host.store.call(check)
        self.assertTrue((self.original / "source.txt").is_file())
        self.assertTrue(all(path.is_file() for path in self.artifact_files))
        self.assertNotIn("p-agent", self.host.definitions)
        self.assertIn("other-agent", self.host.definitions)
        self.assertIn("global", self.host.definitions)
        self.assertNotIn("p-server", self.host.mcp.configurations)
        self.assertIn("other-server", self.host.mcp.configurations)
        self.assertNotIn("p-skill", self.host.skills.definitions)
        self.assertIn("other-skill", self.host.skills.definitions)

    async def test_exact_name_required_and_rename_changes_confirmation(self):
        for name in ("wrong", " Example", "example"):
            with self.subTest(name=name), self.assertRaises(RuntimeFault) as invalid:
                await self.delete(confirm_name=name)
            self.assertEqual(invalid.exception.code, "CONFIRMATION_REQUIRED")
        await self.host.projects.update("p", "Renamed")
        with self.assertRaises(RuntimeFault):
            await self.delete()
        self.assertTrue((await self.delete(confirm_name="Renamed"))["deleted"])

    async def test_original_key_replays_after_restart_and_does_not_delete_recreated_id(self):
        receipt = await self.delete()
        self.assertEqual(await self.delete(), receipt)
        await self.host.aclose()
        self.host = await Host(self.root / "state").__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        self.assertEqual(await self.delete(), receipt)
        await self.host.projects.create("New project", project_id="p")
        with patch.object(self.host, "on_project_deleted", new_callable=AsyncMock) as cleanup:
            self.assertEqual(await self.delete(), receipt)
            cleanup.assert_not_awaited()
        self.assertEqual((await self.host.projects.get("p"))["name"], "New project")
        with self.assertRaises(RuntimeFault) as conflict:
            await self.delete(confirm_name="New project")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        newer = await self.delete(confirm_name="New project", idempotency_key="new-delete-key")
        self.assertNotEqual(newer, receipt)
        self.assertEqual(await self.delete(), receipt)

    async def test_concurrent_same_key_deletes_once_and_returns_one_receipt(self):
        first, second = await asyncio.gather(self.delete(), self.delete())
        self.assertEqual(first, second)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE project_id='p' AND kind='project.deleted'")["n"]), 1)

    async def test_confirmed_deletion_revokes_unfinished_work_and_discards_unsettled_ledgers(self):
        def pending(db):
            db.execute("UPDATE runs SET state='needs_review' WHERE id='p-run'")
            db.execute("UPDATE workflow_runs SET state='waiting' WHERE id='p-workflow_run'")
            db.execute("UPDATE actions SET state='needs_review' WHERE id='p-control'")
            db.execute("UPDATE usage SET state='needs_review' WHERE id='p-usage'")
        await self.host.store.call(pending, transaction=True)
        receipt = await self.delete()
        self.assertTrue(receipt["deleted"])
        self.assertEqual(receipt["pending_actions_discarded"], 1)
        self.assertEqual(receipt["unsettled_usage_discarded"], 1)
        self.assertIsNone(await self.host.store.call(lambda db: one(db, "SELECT * FROM usage WHERE id='p-usage'")))
        self.assertEqual((await self.host.runs.get("other-run"))["state"], "completed")

    async def test_unstarted_upload_and_control_metadata_are_removable_and_other_work_is_unrelated(self):
        await self.host.store.call(lambda db: db.execute("INSERT INTO artifact_uploads SELECT 'pending-upload',project_id,session_id,consumer_id,'pending-key',input_hash,name,bytes,sha256,media_type,created_at FROM artifact_uploads WHERE id='p-upload'"), transaction=True)
        await self.host.store.call(lambda db: db.execute("INSERT INTO tool_intents SELECT 'pending-intent',project_id,consumer_id,name,binding_json,'pending-key',input_hash,input_json,NULL,created_at FROM tool_intents WHERE id='p-intent'"), transaction=True)
        await self.host.store.call(lambda db: db.execute("UPDATE runs SET state='needs_review' WHERE id='other-run'"), transaction=True)
        self.assertTrue((await self.delete())["deleted"])
        self.assertEqual((await self.host.runs.get("other-run"))["state"], "needs_review")

    async def test_foreign_reference_rolls_back_the_entire_delete(self):
        await self.host.store.call(lambda db: db.execute("UPDATE instances SET agent_id='p-agent' WHERE project_id='other'"), transaction=True)
        before = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM conversation")["n"])
        with self.assertRaises(RuntimeFault) as referenced:
            await self.delete()
        self.assertEqual(referenced.exception.code, "PROJECT_REFERENCED")
        self.assertEqual((await self.host.projects.get("p"))["name"], "Example")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM conversation")["n"]), before)
        self.assertEqual(await self.host.store.call(lambda db: rows(db, "PRAGMA foreign_key_check")), [])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE kind='project.deleted'")["n"]), 0)

    async def test_session_archived_filter_paginates_each_view_in_the_database(self):
        for index in range(5):
            session = await self.host.sessions.create("p", "Example", session_id="extra-" + str(index))
            if index % 2:
                await self.host.sessions.set_archived(session["id"], "p", True)
        async def pages(archived):
            result, cursor = [], 0
            while True:
                page = await self.host.sessions.list("p", archived=archived, cursor=cursor, limit=1)
                result.extend(page["items"])
                if page["cursor"] is None:
                    return result
                cursor = page["cursor"]
        active, archived = await pages(False), await pages(True)
        self.assertEqual(len(active), 4)
        self.assertEqual(len(archived), 2)
        self.assertTrue(all(not item["archived"] for item in active))
        self.assertTrue(all(item["archived"] for item in archived))
        self.assertTrue(set(item["id"] for item in active).isdisjoint(item["id"] for item in archived))
        self.assertEqual(len((await self.host.sessions.list("p"))["items"]), 6)
        with self.assertRaises(RuntimeFault):
            await self.host.sessions.list("p", archived=1)


    async def test_legacy_project_archive_field_is_retired_from_catalog_and_dtos(self):
        await self.host.store.call(lambda db: db.execute("UPDATE projects SET archived=1 WHERE id='p'"), transaction=True)
        for value in (await self.host.projects.get("p"), await self.host.projects.create("Example", project_id="p", folder=self.original),
                      await self.host.projects.update("p", "Example"), (await self.host.projects.list())["items"][0]):
            self.assertNotIn("archived", value)
        session = await self.host.sessions.create("p", session_id="legacy-project-session")
        instance = await self.host.instances.create(session["id"], "p-agent", name="legacy-project-worker")
        self.assertEqual(instance["project_id"], "p")
        self.assertNotIn("projects.set_archived", self.host.registry.handlers)
        self.assertIn("projects.delete", self.host.registry.handlers)


    async def test_same_key_commit_during_idle_check_is_read_back(self):
        second_entered, first_finished = asyncio.Event(), asyncio.Event()
        original_idle, original_cleanup = self.host.prepare_project_deletion, self.host.on_project_deleted
        count = 0
        async def idle(project_id):
            nonlocal count
            count += 1
            if count == 1:
                await second_entered.wait()
            else:
                second_entered.set()
                await first_finished.wait()
            await original_idle(project_id)
        async def cleanup(project_id):
            await original_cleanup(project_id)
            first_finished.set()
        with patch.object(self.host, "prepare_project_deletion", idle), patch.object(self.host, "on_project_deleted", cleanup):
            first, second = await asyncio.wait_for(asyncio.gather(self.delete(), self.delete()), timeout=10)
        self.assertEqual(first, second)

    async def test_deletion_cancels_only_its_project_discovery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def list_tools(**options):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return SimpleNamespace(tools=[], nextCursor=None)
        connection = SimpleNamespace(session=SimpleNamespace(list_tools=list_tools))
        with patch.object(self.host.mcp, "connection", new_callable=AsyncMock, return_value=connection):
            task = asyncio.create_task(self.host.mcp.discover("p-server", "p"))
            other = asyncio.create_task(self.host.mcp.discover("other-server", "other"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                self.assertTrue((await self.delete())["deleted"])
                self.assertTrue(task.cancelled())
                self.assertFalse(other.done())
            finally:
                release.set()
                await asyncio.gather(task, other, return_exceptions=True)

    async def test_deletion_cancels_live_upload_without_publishing_partial_bytes(self):
        payload = b"generated upload"
        upload = await self.host.artifacts.begin_upload("p", "new.txt", len(payload), hashlib.sha256(payload).hexdigest(), "live-upload", consumer_id="controller")
        entered, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            entered.set()
            await release.wait()
            yield payload
        task = asyncio.create_task(self.host.artifacts.write_upload(upload["id"], "p", chunks(), consumer_id="controller"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertTrue((await self.delete())["deleted"])
            self.assertTrue(task.cancelled())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse((self.host.root / "artifacts" / upload["id"]).exists())

    async def test_deletion_stops_an_unfinished_host_job_even_if_its_row_is_terminal(self):
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        self.host.jobs["p-run"] = task
        try:
            self.assertTrue((await self.delete())["deleted"])
            self.assertTrue(task.cancelled())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            self.host.jobs.pop("p-run", None)


    async def test_deletion_cancels_controller_preparation_before_an_action_exists(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def preparing(*args, **options):
            entered.set()
            await release.wait()
            return {"state": "not_dispatched"}
        with patch.object(self.host.mcp, "_call_current", preparing):
            task = asyncio.create_task(self.host.mcp.call("p-server", "example", {}, "preparation-key", project_id="p", consumer_id="controller"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                self.assertTrue((await self.delete())["deleted"])
                self.assertTrue(task.cancelled())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_late_uncancellable_call_cannot_recreate_deleted_records(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class DelayedTool:
            def describe(self):
                return ToolSpec(name="delayed", side_effects="external", cancellation="none")
            async def invoke(self, request):
                entered.set()
                await release.wait()
                return {"observed": True}
        self.host.register_tool(DelayedTool())
        await self.host.store.call(lambda db: db.execute("UPDATE runs SET state='running' WHERE id='p-run'"), transaction=True)
        with patch.object(self.host.tools, "authorize", new_callable=AsyncMock), patch.object(self.host.tools, "record_usage", None):
            call = asyncio.create_task(self.host.tools.invoke("p-run", "delayed", {}, "delayed-call"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                receipt = await self.delete()
                self.assertTrue(receipt["deleted"])
                self.assertEqual(receipt["pending_actions_discarded"], 1)
                self.assertFalse(call.done())
                release.set()
                with self.assertRaises(RuntimeFault) as retired:
                    await call
                self.assertEqual(retired.exception.code, "OWNER_DELETED")
                self.assertEqual(await self.host.store.call(lambda db: rows(db, "SELECT * FROM actions WHERE run_id='p-run'")), [])
                self.assertEqual(await self.host.store.call(lambda db: [row["kind"] for row in rows(db, "SELECT kind FROM events WHERE project_id='p'")]), ["project.deleted"])
            finally:
                release.set()
                await asyncio.gather(call, return_exceptions=True)

    async def test_checkpoint_cleanup_can_resume_after_primary_deletion_commits(self):
        await self.host.saver.conn.execute("INSERT INTO checkpoints(thread_id,checkpoint_ns,checkpoint_id,type,checkpoint,metadata) VALUES('p-run','','checkpoint','json',?,?)", (b"{}", b"{}"))
        await self.host.saver.conn.execute("INSERT INTO checkpoints(thread_id,checkpoint_ns,checkpoint_id,type,checkpoint,metadata) VALUES('other-run','','checkpoint','json',?,?)", (b"{}", b"{}"))
        await self.host.saver.conn.commit()
        with patch.object(self.host.saver, "adelete_thread", new_callable=AsyncMock, side_effect=RuntimeError("Interrupted cleanup")):
            with self.assertRaises(RuntimeError):
                await self.delete()
        self.assertIsNone(await self.host.store.call(lambda db: one(db, "SELECT * FROM projects WHERE id='p'")))
        pending = await self.host.store.call(lambda db: one(db, "SELECT json_extract(data_json,'$.cleanup_threads') AS threads FROM events WHERE project_id='p'"))
        self.assertIn("p-run", pending["threads"])
        await self.host.aclose()
        self.host = await Host(self.root / "state").__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        self.assertTrue((await self.delete())["deleted"])
        async with self.host.saver.conn.execute("SELECT thread_id FROM checkpoints ORDER BY thread_id") as cursor:
            self.assertEqual(await cursor.fetchall(), [("other-run",)])
        self.assertIsNone((await self.host.store.call(lambda db: one(db, "SELECT json_extract(data_json,'$.cleanup_threads') AS threads FROM events WHERE project_id='p'")))["threads"])

    async def test_wrong_confirmation_does_not_cancel_current_work(self):
        task = asyncio.create_task(asyncio.Event().wait())
        self.host.jobs["p-run"] = task
        try:
            with self.assertRaises(RuntimeFault):
                await self.delete(confirm_name="Wrong")
            self.assertFalse(task.done())
            self.assertNotIn("p", self.host.project_deletion_scopes)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.host.jobs.pop("p-run", None)


if __name__ == "__main__":
    unittest.main()
