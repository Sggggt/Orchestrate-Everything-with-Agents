"""Application execution uses the existing graph with isolated ownership and grants."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Instructions, Model, ProjectWorkspace, RunSpec, Tools, Workspace
from oea.contracts.applications import ApplicationBinding, ApplicationGrant, ApplicationRun, ApplicationResume, ConversationCreate
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.workspace.environment import ExecutionEnvironment
from test_host import BarrierModel, WaitModel


class ApplicationExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-application-execution-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        self.model = BarrierModel()
        self.host = await Host(self.root / "state", allow_commands=True, model_api=self.model).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.projects.create("Example", project_id="p", folder=self.project_dir,
            workspace=ProjectWorkspace(network="allow", environment={"PRIVATE_SETTING": "local"}))
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid", model="fixture"),
            tools=Tools(local=("exec_command", "write_stdin", "apply_patch", "publish_artifact")),
            instructions=Instructions(agent_md="Private project instructions"))
        await self.host.agents.save(self.definition)
        self.application = await self.host.applications.create("Example")
        self.app_id = self.application["application"]["id"]
        self.binding_spec = ApplicationBinding(app_id=self.app_id, project_id="p", agent_id="worker",
            grant=ApplicationGrant(tools=self.definition.tools))
        self.binding = await self.host.applications.bind(self.binding_spec)
        self.conversation = await self.host.oap.create_conversation(self.app_id, ConversationCreate(binding_id=self.binding["id"]), "conversation")

    async def submit(self, key="work"):
        return await self.host.oap.submit(self.app_id, self.conversation["id"], ApplicationRun(input="Complete the work"), key)

    async def wait_state(self, run_id, expected):
        async with asyncio.timeout(10):
            while True:
                signal = self.host.run_signal(run_id)
                value = await self.host.runs.get(run_id)
                if value["state"] == expected:
                    return value
                if value["state"] in {"failed", "needs_review", "cancelled"}:
                    self.fail(str(value))
                await signal.wait()

    async def test_three_scopes_have_independent_instances_workspaces_and_instructions(self):
        self.model.target = 3
        session = await self.host.sessions.create("p")
        normal = await self.host.instances.create(session["id"], "worker")
        ordinary = await self.host.runs.submit(RunSpec(instance_id=normal["id"], input="Normal work", idempotency_key="same"))
        first = await self.submit()
        second_app = await self.host.applications.create("Second")
        second_id = second_app["application"]["id"]
        second_binding = await self.host.applications.bind(self.binding_spec.model_copy(update={"app_id": second_id}))
        second_conversation = await self.host.oap.create_conversation(second_id, ConversationCreate(binding_id=second_binding["id"]), "conversation")
        second = await self.host.oap.submit(second_id, second_conversation["id"], ApplicationRun(input="Other work"), "work")
        await asyncio.wait_for(self.model.ready.wait(), 10)
        contexts = [self.host.active[handle.id] for handle in (ordinary, first, second)]
        self.assertEqual(len({context.paths.work_dir for context in contexts}), 3)
        self.assertEqual(contexts[0].paths.project_dir, self.project_dir)
        for context in contexts[1:]:
            self.assertIsNone(context.paths.project_dir)
            self.assertEqual(context.paths.temp_dir, context.paths.work_dir.parent / "temp")
            self.assertEqual(context.definition.workspace.environment, {})
            self.assertEqual(context.definition.workspace.network, "deny")
            instructions = await context.instructions()
            self.assertNotIn("Private project instructions", instructions)
            self.assertNotIn(str(self.project_dir), instructions)
            self.assertEqual(context.coordination_tools(), set())
            for other in contexts:
                if other is not context:
                    with self.assertRaises(RuntimeFault):
                        context.paths.resolve(str(other.paths.work_dir / "source.txt"))
            with self.assertRaises(RuntimeFault):
                context.paths.resolve(str(context.paths.input_dirs[0] / "source.txt"), write=True)
        boards = await self.host.store.call(lambda db: rows(db, "SELECT * FROM boards WHERE session_id IN (SELECT session_id FROM application_conversations)"))
        self.assertEqual(boards, [])
        with self.assertRaises(RuntimeFault):
            await self.host.oap.run(self.app_id, second.id)
        self.model.release.set()
        await asyncio.gather(*(self.host.runs.result(handle.id, timeout=10) for handle in (ordinary, first, second)))

    async def test_retries_reuse_first_instance_and_closed_conversation_keeps_receipts(self):
        first, second = await asyncio.gather(self.submit(), self.submit())
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.instance_id, second.instance_id)
        self.assertTrue(first.reused or second.reused)
        count = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM instances"))
        self.assertEqual(count["n"], 1)
        await self.host.oap.close_conversation(self.app_id, self.conversation["id"], "close")
        self.assertEqual((await self.submit()).id, first.id)
        with self.assertRaises(RuntimeFault) as fault:
            await self.submit("new")
        self.assertEqual(fault.exception.code, "CONVERSATION_CLOSED")

    async def test_communication_denied_at_discovery_and_invocation(self):
        functions = ("list_members", "assign_work")
        await self.host.agents.save(self.definition.model_copy(update={"tools": self.definition.tools.model_copy(update={"functions": functions})}), expected_revision=1)
        explicit = self.binding_spec.model_copy(update={"grant": self.binding_spec.grant.model_copy(update={
            "trusted_extensions": True, "tools": self.definition.tools.model_copy(update={"functions": functions})})})
        await self.host.applications.bind(explicit, binding_id=self.binding["id"], expected_revision=1)
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        context = self.host.active[run.id]
        from oea.collaboration.coordination import COORDINATION_TOOLS
        self.assertFalse(set(context.tool_names()) & COORDINATION_TOOLS)
        for name in COORDINATION_TOOLS:
            with self.assertRaises(RuntimeFault):
                await self.host.authorize(run.id, name)
        with self.assertRaises(RuntimeFault):
            await self.host.store.call(lambda db: self.host.coordination.actor(db, run.id))
        with self.assertRaises(RuntimeFault):
            await self.host.boards.create("p", "Forbidden", session_id=context.instance["session_id"])

    async def test_application_work_blocks_architecture_switch_without_business_identifiers(self):
        from oea import AgentArchitecture, ArchitectureBinding
        from oea.orchestration.architecture_templates import react
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        await self.host.architectures.create(AgentArchitecture(id="candidate", project_id="p", name="Candidate"), react().model_dump(mode="json"))
        version = await self.host.architectures.publish("candidate", "p", 1)
        binding = ArchitectureBinding(architecture_id="candidate", version=version["version"])
        status = await self.host.architectures.status("worker", "p")
        self.assertEqual(status["blocker"], {"source": "application", "pending": True})
        with self.assertRaises(RuntimeFault) as blocked:
            await self.host.architectures.bind("worker", "p", binding, expected_revision=1)
        self.assertEqual(blocked.exception.code, "AGENT_IN_USE")
        self.assertEqual(blocked.exception.details, {"source": "application", "pending": True})
        self.model.release.set()
        await self.host.runs.result(run.id, timeout=10)
        self.assertIsNone((await self.host.architectures.status("worker", "p"))["blocker"])
        await self.host.architectures.bind("worker", "p", binding, expected_revision=1)

    async def test_current_binding_and_application_revocation_apply_to_active_run(self):
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        await self.host.authorize(run.id, "exec_command")
        narrowed = self.binding_spec.model_copy(update={"grant": ApplicationGrant()})
        await self.host.applications.bind(narrowed, binding_id=self.binding["id"], expected_revision=1)
        with self.assertRaises(RuntimeFault):
            await self.host.authorize(run.id, "exec_command")
        await self.host.applications.set_enabled(self.app_id, False, 1)
        with self.assertRaises(RuntimeFault):
            await self.host.authorize_model(self.host.active[run.id])

    async def test_project_directory_narrowing_refreshes_an_active_application_scope(self):
        shared = self.root / "shared"
        kept, removed = shared / "kept", shared / "removed"
        for folder in (kept, removed):
            folder.mkdir(parents=True)
            (folder / "input.txt").write_text(folder.name, encoding="utf-8")
        await self.host.projects.update("p", "Example", workspace=ProjectWorkspace(references=(shared,)), expected_revision=1)
        grant = self.binding_spec.grant.model_copy(update={"workspace": Workspace(inherit_project=False, references=(shared,))})
        await self.host.applications.bind(self.binding_spec.model_copy(update={"grant": grant}), binding_id=self.binding["id"], expected_revision=1)
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        before, _ = await self.host.resolve_workspace(run.id)
        self.assertEqual(before.resolve(str(removed / "input.txt")), removed / "input.txt")
        await self.host.projects.update("p", "Example", workspace=ProjectWorkspace(references=(kept,)), expected_revision=2)
        after, _ = await self.host.resolve_workspace(run.id)
        self.assertEqual(after.work_dir, before.work_dir)
        with after.open_read(str(kept / "input.txt")) as source:
            self.assertEqual(source.read(), b"kept")
        with self.assertRaises(RuntimeFault) as denied:
            after.resolve(str(removed / "input.txt"))
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

    async def test_required_skill_needs_an_application_grant_and_cached_reads_observe_revocation(self):
        from oea import SkillBinding, SkillSource
        source = self.root / "private-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("Granted private instructions.", encoding="utf-8")
        await self.host.skills.register(SkillSource(id="private", project_id="p", path=source), expected_revision=0)
        definition = self.definition.model_copy(update={"skills": (SkillBinding(id="private", required=True),)})
        await self.host.agents.save(definition, expected_revision=1)
        with self.assertRaises(RuntimeFault) as missing:
            await self.submit()
        self.assertEqual(missing.exception.code, "REQUIRED_SKILL_UNAVAILABLE")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM instances")["n"]), 0)
        grant = self.binding_spec.grant.model_copy(update={"skills": ("private",)})
        await self.host.applications.bind(self.binding_spec.model_copy(update={"grant": grant}), binding_id=self.binding["id"], expected_revision=1)
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        context = self.host.active[run.id]
        original = await context.read_skill("private")
        self.assertEqual(original["text"], "Granted private instructions.")
        self.assertEqual((await context.read_skill("private"))["reference"], original["reference"])
        await self.host.applications.bind(self.binding_spec, binding_id=self.binding["id"], expected_revision=2)
        with self.assertRaises(RuntimeFault) as revoked:
            await context.read_skill("private")
        self.assertEqual(revoked.exception.code, "REQUIRED_SKILL_UNAVAILABLE")

    async def test_resume_preserves_directory_and_does_not_restore_project_capabilities(self):
        self.host.model_api = WaitModel()
        run = await self.submit()
        await self.wait_state(run.id, "waiting")
        before = await self.host.instances.get(run.instance_id)
        narrowed = self.binding_spec.model_copy(update={"grant": ApplicationGrant()})
        await self.host.applications.bind(narrowed, binding_id=self.binding["id"], expected_revision=1)
        await self.host.oap.resume(self.app_id, run.id, ApplicationResume(input="37"), "reply")
        result = await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(result.text, "Answer: 37")
        after = await self.host.instances.get(run.instance_id)
        self.assertEqual(before["workspace"]["work_dir"], after["workspace"]["work_dir"])
        saved = await self.host.store.call(lambda db: one(db, "SELECT config_json,budget_id FROM runs WHERE id=?", (run.id,)))
        config = json.loads(saved["config_json"])
        self.assertIsNone(config["workspace"]["project"])
        self.assertEqual(config["instructions"]["agent_md"], "")
        self.assertEqual(config["tools"]["local"], [])
        self.assertEqual(saved["budget_id"], run.budget_id)

    async def test_declarative_host_function_needs_explicit_trusted_extension_grant(self):
        from oea import AgentArchitecture, ArchitectureBinding, ArchitectureBuilder
        from oea.orchestration.architecture_templates import react
        calls = []
        def compute() -> dict:
            calls.append("computed")
            return {}
        self.host.workflows.register_function(compute)
        graph = ArchitectureBuilder(react()).node("compute", "function", name="compute")
        graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare"]
        graph.connect("prepare", "compute").connect("compute", "model")
        await self.host.architectures.create(AgentArchitecture(id="extension", project_id="p", name="Extension"), graph.build().model_dump(mode="json"))
        await self.host.architectures.publish("extension", "p", 1)
        await self.host.architectures.bind("worker", "p", ArchitectureBinding(architecture_id="extension", version=1), 1)
        with self.assertRaises(RuntimeFault) as fault:
            await self.submit()
        self.assertEqual(fault.exception.code, "PERMISSION_DENIED")
        self.assertEqual(calls, [])
        self.assertIsNone((await self.host.oap.conversation(self.app_id, self.conversation["id"]))["instance_id"])
        granted = self.binding_spec.model_copy(update={"grant": self.binding_spec.grant.model_copy(update={"trusted_extensions": True})})
        await self.host.applications.bind(granted, binding_id=self.binding["id"], expected_revision=1)
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        self.assertEqual(calls, ["computed"])
        self.model.release.set()
        await self.host.runs.result(run.id, timeout=10)


    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
    async def test_native_command_uses_private_temp_and_stops_on_revocation(self):
        native = patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"]))
        native.start()
        self.addCleanup(native.stop)
        run = await self.submit()
        await asyncio.wait_for(self.model.ready.wait(), 10)
        paths, settings = await self.host.resolve_workspace(run.id)
        receipt = await self.host.command_sessions.start({"run_id": run.id, "action_id": "application-command",
            "arguments": {"argv": [sys.executable, "-B", "-u", "-c",
                "import os,time; from pathlib import Path; print(os.environ['TEMP']); Path('owned.txt').write_text('owned'); time.sleep(30)"],
                "yield_time_ms": 500}}, ExecutionEnvironment(paths, settings))
        self.assertEqual(receipt["state"], "running", receipt)
        async with asyncio.timeout(10):
            while not receipt["stdout"]:
                receipt = await self.host.command_sessions.observe(run.id, {"command_session_id": receipt["command_session_id"], "yield_time_ms": 100})
                self.assertEqual(receipt["state"], "running", receipt)
                self.assertFalse(receipt["stderr"], receipt)
        await self.host.applications.set_enabled(self.app_id, False, 1)
        observed = await self.host.command_sessions.observe(run.id, {"command_session_id": receipt["command_session_id"]})
        self.assertTrue(observed["process_stopped"])
        self.assertEqual(observed["termination"], "permission_changed")
        self.assertTrue(Path(observed["stdout"].strip()).resolve().is_relative_to(paths.temp_dir))
        self.assertEqual((paths.work_dir / "owned.txt").read_text(), "owned")

    @unittest.skipUnless(os.environ.get("OEA_TEST_NATIVE_COMPONENT"), "Native component is not configured")
    async def test_native_three_scope_files_inputs_environment_and_child_processes(self):
        native = patch("oea.workspace.native.executable", return_value=Path(os.environ["OEA_TEST_NATIVE_COMPONENT"]))
        native.start()
        self.addCleanup(native.stop)
        self.model.target = 3
        session = await self.host.sessions.create("p")
        ordinary = await self.host.instances.create(session["id"], "worker")
        normal = await self.host.runs.submit(RunSpec(instance_id=ordinary["id"], input="Normal", idempotency_key="normal"))
        first = await self.submit()
        other = await self.host.applications.create("Other")
        other_id = other["application"]["id"]
        binding = await self.host.applications.bind(self.binding_spec.model_copy(update={"app_id": other_id}))
        conversation = await self.host.oap.create_conversation(other_id, ConversationCreate(binding_id=binding["id"]), "conversation")
        second = await self.host.oap.submit(other_id, conversation["id"], ApplicationRun(input="Other"), "work")
        await asyncio.wait_for(self.model.ready.wait(), 10)
        handles = (normal, first, second)
        contexts = [self.host.active[handle.id] for handle in handles]
        private = self.project_dir / "private.txt"
        private.write_text("project original", encoding="utf-8")
        for index, context in enumerate(contexts):
            (context.paths.work_dir / "scope.txt").write_text(str(index), encoding="utf-8")
            for directory in context.paths.input_dirs:
                (directory / "assigned.txt").write_text("assigned", encoding="utf-8")
        async def check(index, context):
            targets = [str(item.paths.work_dir / "scope.txt") for item in contexts]
            code = """import json,os,subprocess,sys
from pathlib import Path
result={'reads':[],'writes':[],'secret':os.environ.get('OEA_TEST_SECRET'),'setting':os.environ.get('PRIVATE_SETTING')}
for name in json.loads(sys.argv[1]):
    try: result['reads'].append(Path(name).read_text())
    except OSError: result['reads'].append(None)
    try: Path(name).with_name('cross-write.txt').write_text('written');result['writes'].append(True)
    except OSError: result['writes'].append(False)
try: result['project']=Path(sys.argv[2]).read_text()
except OSError: result['project']=None
if sys.argv[3]:
    result['input']=Path(sys.argv[3]).read_text()
    try: Path(sys.argv[3]).write_text('changed');result['input_write']=True
    except OSError: result['input_write']=False
result['child']=subprocess.check_output([sys.executable,'-B','-c',"print('child')"],timeout=5).decode().strip()
print(json.dumps(result))
"""
            assigned = str(context.paths.input_dirs[0] / "assigned.txt") if context.paths.input_dirs else ""
            receipt = await self.host.tools.invoke(context.run["id"], "exec_command",
                {"argv": [sys.executable, "-B", "-u", "-c", code, json.dumps(targets), str(private), assigned], "yield_time_ms": 1000}, "scope-check")
            attempt = 0
            async with asyncio.timeout(15):
                while receipt["state"] == "running":
                    attempt += 1
                    receipt = await self.host.tools.invoke(context.run["id"], "write_stdin",
                        {"command_session_id": receipt["command_session_id"], "yield_time_ms": 1000}, "scope-observe-" + str(attempt))
            self.assertEqual(receipt["exit_code"], 0, receipt)
            observed = json.loads(receipt["stdout"])
            self.assertEqual(observed["reads"], [str(index) if number == index else None for number in range(3)])
            self.assertEqual(observed["writes"], [number == index for number in range(3)])
            self.assertIsNone(observed["secret"])
            self.assertEqual(observed["child"], "child")
            self.assertEqual(observed["project"], "project original" if index == 0 else None)
            self.assertEqual(observed["setting"], "local" if index == 0 else None)
            if index:
                self.assertEqual(observed["input"], "assigned")
                self.assertFalse(observed["input_write"])
        with patch.dict(os.environ, {"OEA_TEST_SECRET": "must-not-be-inherited"}):
            checked = await asyncio.gather(*(check(index, context) for index, context in enumerate(contexts)), return_exceptions=True)
        self.model.release.set()
        await asyncio.gather(*(self.host.runs.result(handle.id, timeout=10) for handle in handles))
        for result in checked:
            if isinstance(result, BaseException):
                raise result
