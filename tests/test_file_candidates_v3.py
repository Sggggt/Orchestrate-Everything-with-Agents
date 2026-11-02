"""Unpublished files have scoped, persistent observations and explicit verification."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Completion, Model, RunSpec, Tools, Workspace
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class FileModel:
    def __init__(self, target="draft.md"):
        self.target, self.calls = target, 0
        self.publish = False

    async def complete(self, *args, **options):
        self.calls += 1
        if self.calls == 1:
            name, arguments = "apply_patch", {"path": self.target, "operation": "add", "content": "Draft version one"}
        elif self.calls == 2:
            code = "import os,pathlib; p=pathlib.Path(os.environ['OEA_OUTPUT_DIR']); (p/'nested').mkdir(); [(p/('file-'+str(i)+'.txt')).write_text(str(i)) for i in range(4)]; (p/'nested'/'child.txt').write_text('child')"
            name, arguments = "exec_command", {"argv": [sys.executable, "-B", "-c", code], "yield_time_ms": 1000}
        elif self.calls == 3 and self.publish:
            name, arguments = "publish_artifact", {"path": self.target}
        else:
            return Completion(text="Draft saved; not published or accepted")
        return Completion(tool_calls=({"id": "file-" + str(self.calls), "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}},), finish_reason="tool_calls")


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    sandbox = "trusted"

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-candidates-")
        self.root = Path(self.temporary.name)
        self.model = FileModel()
        self.host = await Host(self.root / "state", model_api=self.model, allow_commands=True).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        self.definition = AgentDefinition(id="writer", name="Writer", project_id="p",
            model=Model(base_url="https://model.invalid", model="fixture"),
            tools=Tools(local=("apply_patch", "exec_command", "publish_artifact")), workspace=Workspace(sandbox=self.sandbox))
        await self.host.agents.save(self.definition)
        session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(session["id"], "writer")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def run_files(self):
        run = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Save draft files", idempotency_key="files"))
        result = await self.host.runs.result(run.id, timeout=15)
        self.assertEqual(len(result.artifacts), 1 if self.model.publish else 0)
        self.work = self.host.root / "workspaces" / self.instance["id"]
        self.outputs = self.work / "outputs" / run.id
        return run

    async def test_explicit_publication_is_reported_separately_from_the_candidate(self):
        self.model.publish = True
        run = await self.run_files()
        candidate = (await self.host.artifacts.candidates(run.id, "p"))["items"][0]
        artifacts = await self.host.artifacts.for_run(run.id)
        self.assertTrue(candidate["published"])
        self.assertEqual(candidate["published_artifact_id"], artifacts[0]["id"])

    async def test_control_result_pages_keep_observations_without_rechecking_files(self):
        run = await self.run_files()
        params = {"run_id": run.id, "project_id": "p"}
        page = await self.host.registry.call("artifacts.candidates", params)
        candidate = page["items"][0]
        saved = await self.host.results.issue("artifacts.candidates", params, page, "reader", characters=0)
        single_params = {"reference": candidate["reference"], "project_id": "p", "check_source": True}
        verified = await self.host.registry.call("artifacts.candidate", single_params)
        single = await self.host.results.issue("artifacts.candidate", single_params, verified, "reader", characters=0)
        (self.work / "draft.md").write_text("Changed after the original response")
        with patch("oea.workspace.environment.ExecutionEnvironment.observe_files", side_effect=AssertionError("Do not repeat a file observation for result paging")):
            value = await self.host.results.read(saved["result_ref"], project_id="p", consumer_id="reader", json_pointer="/items/0/sha256")
            self.assertEqual(value["value"], candidate["sha256"])
            original = await self.host.results.read(single["result_ref"], project_id="p", consumer_id="reader", json_pointer="/source_state")
            self.assertEqual(original["value"], "unchanged")
        with self.assertRaises(RuntimeFault):
            await self.host.results.read(saved["result_ref"], project_id="q", consumer_id="reader")
        self.assertEqual(self.model.calls, 3)

    async def test_patch_versions_survive_restart_and_detect_changes_without_publication(self):
        run = await self.run_files()
        page = await self.host.artifacts.candidates(run.id, "p")
        candidate = page["items"][0]
        self.assertFalse(candidate["published"])
        checked = await self.host.artifacts.candidate(candidate["reference"], "p", check_source=True)
        self.assertEqual(checked["source_state"], "unchanged")
        self.assertEqual(checked["current_reference"], candidate["reference"])
        (self.work / "draft.md").write_text("A changed draft", encoding="utf-8")
        changed = await self.host.artifacts.candidate(candidate["reference"], "p", check_source=True)
        self.assertEqual(changed["source_state"], "changed")
        self.assertTrue(changed["review_required"])
        self.assertEqual(changed["sha256"], candidate["sha256"])
        self.assertNotEqual(changed["current"]["sha256"], candidate["sha256"])
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=self.model, allow_commands=True).__aenter__()
        original = await self.host.artifacts.candidate(candidate["reference"], "p")
        self.assertEqual(original["sha256"], candidate["sha256"])
        self.assertEqual(self.model.calls, 3)
        self.assertEqual(await self.host.artifacts.for_run(run.id), [])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) n FROM runs")["n"]), 1)
        (self.work / "draft.md").unlink()
        self.assertEqual((await self.host.artifacts.candidate(candidate["reference"], "p", check_source=True))["source_state"], "missing")

    async def test_output_directory_is_bounded_paged_and_version_checked(self):
        run = await self.run_files()
        (self.work / "private-work-file.txt").write_text("Outside output listing")
        first = await self.host.artifacts.candidates(run.id, "p", source="outputs", limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNone(first["items"][0]["sha256"])
        verified = await self.host.artifacts.candidate(first["items"][0]["reference"], "p", check_source=True)
        self.assertEqual(verified["source_state"], "first_verification")
        self.assertEqual((await self.host.artifacts.candidate(verified["current_reference"], "p", check_source=True))["source_state"], "unchanged")
        second = await self.host.artifacts.candidates(run.id, "p", source="outputs", cursor=first["cursor"], limit=2, version=first["version"])
        self.assertFalse({item["path"] for item in first["items"]} & {item["path"] for item in second["items"]})
        children = await self.host.artifacts.candidates(run.id, "p", source="outputs", directory="nested")
        self.assertEqual([item["name"] for item in children["items"]], ["child.txt"])
        (self.outputs / "new.txt").write_text("new")
        with self.assertRaises(RuntimeFault) as changed:
            await self.host.artifacts.candidates(run.id, "p", source="outputs", cursor=first["cursor"], version=first["version"])
        self.assertEqual(changed.exception.code, "SOURCE_VERSION_CHANGED")
        with self.assertRaises(RuntimeFault) as escaped:
            await self.host.artifacts.candidates(run.id, "p", source="outputs", directory="../..")
        self.assertEqual(escaped.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(RuntimeFault):
            await self.host.artifacts.candidates(run.id, "q")
        with self.assertRaises(RuntimeFault):
            await self.host.artifacts.candidate(first["items"][0]["reference"], "q", check_source=True)

    async def test_live_verification_obeys_current_directory_grants(self):
        external = self.root / "granted"
        external.mkdir()
        self.model.target = str(external / "draft.md")
        self.definition.workspace.writable_directories = (external,)
        await self.host.agents.save(self.definition, expected_revision=1)
        run = await self.run_files()
        reference = (await self.host.artifacts.candidates(run.id, "p"))["items"][0]["reference"]
        self.definition.workspace.writable_directories = ()
        await self.host.agents.save(self.definition, expected_revision=2)
        self.assertEqual((await self.host.artifacts.candidate(reference, "p"))["source_state"], "not_checked")
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.artifacts.candidate(reference, "p", check_source=True)
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

    async def test_large_directory_reports_a_partial_observation(self):
        run = await self.run_files()
        for index in range(1001):
            (self.outputs / ("entry-" + str(index))).touch()
        page = await self.host.artifacts.candidates(run.id, "p", source="outputs", limit=100)
        self.assertTrue(page["truncated"])
        self.assertLessEqual(len(page["items"]) + len(page["directories"]), 100)
        self.assertEqual(page["version_scope"], "directory_entries")

    @unittest.skipIf(os.name == "nt", "Portable link creation is verified on Linux")
    async def test_output_links_cannot_expose_another_directory(self):
        run = await self.run_files()
        private = self.root / "private"
        private.mkdir()
        (private / "hidden.txt").write_text("not output")
        (self.outputs / "linked").symlink_to(private, target_is_directory=True)
        page = await self.host.artifacts.candidates(run.id, "p", source="outputs")
        self.assertNotIn("linked", page["directories"])
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.artifacts.candidates(run.id, "p", source="outputs", directory="linked")
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")


@unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
class NativeCandidateTests(CandidateTests):
    sandbox = "native"

    async def asyncSetUp(self):
        self.enterContext(patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"])))
        await super().asyncSetUp()
