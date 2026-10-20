"""Project isolation and summary/full-message contracts against a real local Runtime."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from ma_runtime import AgentConfig, AgentDescriptor, AdapterEvent, Runtime, TaskResult
from ma_runtime.errors import RuntimeFault
from ma_runtime.models import json_dump
from ma_runtime.presentation import DETAILS_MARKER, SummaryStream, markdown_summary
from ma_runtime.web.app import create_app


class SummaryTests(unittest.TestCase):
    def test_marker_split_at_every_character_never_leaks_peer_content(self):
        text = "### 完成\n\n检查完成。\n\n" + DETAILS_MARKER + "\nPEER_ONLY_FULL_CONTENT"
        for width in range(1, len(DETAILS_MARKER) + 2):
            stream = SummaryStream()
            shown = "".join(stream.feed(text[i:i+width]) for i in range(0, len(text), width)) + stream.finish()
            self.assertIn("检查完成", shown)
            self.assertNotIn("PEER_ONLY", shown)
            self.assertNotIn("<!--", shown)

    def test_plain_markdown_is_accepted_and_legacy_json_is_summarized(self):
        self.assertEqual(markdown_summary("普通回复，无固定标题"), "普通回复，无固定标题")
        legacy = json.dumps({"stage": "research", "analysis": {"cards": [{"conclusion": "待核对"}]}})
        rendered = markdown_summary(legacy)
        self.assertIn("待核对", rendered)
        self.assertNotIn('"analysis"', rendered)
        self.assertNotIn("{", rendered)
        self.assertNotIn("PEER_ONLY", markdown_summary("摘要\n\n" + DETAILS_MARKER + "PEER_ONLY"))
        self.assertNotIn("待审核", markdown_summary({"status": "review_required"}))

    def test_stream_is_bounded_and_structured_text_is_not_displayed(self):
        stream = SummaryStream(limit=30)
        self.assertLessEqual(len(stream.feed("a" * 10000)), 30)
        self.assertTrue(stream.closed)
        structured = SummaryStream()
        self.assertEqual(structured.feed('{"private":"value"}'), "")
        self.assertNotIn("private", structured.finish())

    def test_fenced_json_after_a_markdown_intro_is_only_for_peers(self):
        text = "### 摘要\n已处理。\n```json\n{\"PEER_ONLY\":true}\n```"
        self.assertNotIn("PEER_ONLY", markdown_summary(text))
        for width in (1, 2, 5, 20):
            stream = SummaryStream()
            shown = "".join(stream.feed(text[i:i+width]) for i in range(0, len(text), width))
            self.assertNotIn("PEER_ONLY", shown + stream.finish())


class ReplyAgent:
    def __init__(self, identity):
        self.identity, self.requests = identity, []

    def describe(self):
        return AgentDescriptor(agent_id=self.identity)

    async def execute(self, request, context):
        self.requests.append(request)
        for piece in ["### 任务\n", "\n检查已完成。\n", "\n<!-- agent:", "details -->\nPEER_ONLY"]:
            yield AdapterEvent(type="progress", payload={"delta": piece})
            await asyncio.sleep(0)
        full = "### 任务\n\n检查已完成。\n\n" + DETAILS_MARKER + "\nPEER_ONLY_FULL_CONTENT"
        yield AdapterEvent(type="completed", result=TaskResult(output={"text": full}))

    async def aclose(self):
        pass


class ProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ma-project-tests-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        self.folder = self.root / "files"
        self.folder.mkdir()
        await self.rt.projects.update("default", name="个人研究", local_folder=str(self.folder))

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def test_create_update_idempotency_and_explicit_project_required(self):
        p = await self.rt.projects.create("其他项目", str(self.folder), "one-project")
        again = await self.rt.projects.create("其他项目", str(self.folder), "one-project")
        self.assertEqual(p.id, again.id)
        with self.assertRaises(RuntimeFault):
            await self.rt.projects.create("冲突", str(self.folder), "one-project")
        with self.assertRaises(RuntimeFault):
            await self.rt.sessions.create(title="Must choose a project")
        with self.assertRaises(RuntimeFault):
            await self.rt.projects.create("Relative", "relative/path", "relative")
        await self.rt.projects.update(p.id, name="已改名")
        self.assertEqual((await self.rt.projects.get(p.id)).name, "已改名")

    async def test_agent_team_changes_and_cross_project_thread_is_rejected(self):
        await self.rt.agents.configure(AgentConfig(id="six", name="研究助手", kind="mock"))
        other = await self.rt.projects.create("其他项目", str(self.folder), "other")
        await self.rt.agents.configure(AgentConfig(id="other", name="其他", kind="mock", project_id=other.id))
        self.assertEqual([a["id"] for a in await self.rt.agents.list("default")], ["six"])
        self.assertEqual([a["id"] for a in await self.rt.agents.list(other.id)], ["other"])
        session = await self.rt.sessions.create(project_id=other.id)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.threads.create(session.id, "six")
        self.assertEqual(error.exception.code, "PROJECT_MISMATCH")
        await self.rt.threads.create(session.id, "other")
        self.assertEqual(len((await self.rt.sessions.list(project_id="default")).items), 0)

    async def test_adopt_moves_history_and_agents_atomically(self):
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock"))
        session = await self.rt.sessions.create(title="保留历史")
        await self.rt.threads.create(session.id, "worker")
        target = await self.rt.projects.create("目标", str(self.folder), "target")
        with self.assertRaises(RuntimeFault):
            await self.rt.projects.adopt(target.id, [session.id], [])
        await self.rt.projects.adopt(target.id, [session.id], ["worker"])
        self.assertEqual((await self.rt.sessions.get(session.id)).title, "保留历史")
        self.assertEqual((await self.rt.sessions.get(session.id)).project_id, target.id)
        self.assertEqual((await self.rt.agents.describe("worker"))["project_id"], target.id)

    async def test_archived_project_is_readable_but_cannot_start_new_work(self):
        await self.rt.projects.archive("default")
        self.assertEqual(len(await self.rt.projects.list()), 0)
        self.assertEqual((await self.rt.projects.get("default")).state, "ARCHIVED")
        with self.assertRaises(RuntimeFault):
            await self.rt.sessions.create(project_id="default")
        await self.rt.projects.archive("default", False)
        await self.rt.sessions.create(project_id="default")

    async def test_full_peer_data_and_history_survive_summary_projection(self):
        await self.rt.agents.configure(AgentConfig(id="writer", name="Writer", kind="mock"))
        adapter = ReplyAgent("writer")
        self.rt.adapters["writer"] = adapter
        session = await self.rt.sessions.create(title="摘要与全文")
        thread = await self.rt.threads.create(session.id, "writer")
        spec = {"session_id": session.id, "thread_id": thread.id, "agent_id": "writer",
                "payload": {"input": "Inspect"}, "idempotency_key": "first"}
        task = await self.rt.tasks.submit(spec)
        result = await self.rt.tasks.result(task.id, timeout=5)
        self.assertIn("PEER_ONLY_FULL_CONTENT", result.output["text"])
        summary = await self.rt.tasks.progress(task.id)
        self.assertNotIn("PEER_ONLY", summary["summary_markdown"])
        snapshot = await self.rt.sessions.snapshot(session.id, presentation=True)
        self.assertNotIn("PEER_ONLY", json_dump(snapshot))
        shown = await self.rt.threads.conversation(thread.id, presentation=True)
        self.assertNotIn("PEER_ONLY", json_dump(shown))
        complete = await self.rt.threads.conversation(thread.id)
        self.assertIn("PEER_ONLY", json_dump(complete))
        next_task = await self.rt.tasks.submit({**spec, "idempotency_key": "second", "depends_on": [task.id]})
        await self.rt.tasks.result(next_task.id, timeout=5)
        request = adapter.requests[-1]
        self.assertIn("PEER_ONLY", json_dump(request.dependency_results))
        self.assertIn("PEER_ONLY", json_dump(request.conversation))
        self.assertEqual(request.project_id, "default")
        self.assertEqual(request.project_folder, str(self.folder.resolve()))

    async def test_http_bootstrap_filters_project_team_and_exposes_project_management(self):
        await self.rt.agents.configure(AgentConfig(id="six", name="研究助手", kind="mock"))
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(base_url="http://127.0.0.1",
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + app.state.token}) as client:
                response = await client.post("/api/projects", json={
                    "name": "Empty", "local_folder": str(self.folder), "idempotency_key": "http"})
                self.assertEqual(response.status_code, 200)
                project = response.json()
                bootstrap = (await client.get("/api/bootstrap", params={"project_id": project["id"]})).json()
                self.assertEqual(bootstrap["agents"], [])
                self.assertEqual(bootstrap["sessions"], [])
                self.assertEqual(bootstrap["project"]["id"], project["id"])
