"""Release checks reject copied private state without hiding useful source assets."""

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).resolve().parents[1] / "scripts/check_release.py"
)
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


class ReleaseHygieneTests(unittest.TestCase):
    def test_copied_runtime_state_and_atomic_write_remnants_are_rejected(self):
        names = (
            "client-token", "client.json", "secrets.enc", "vault.key", "host.lock",
            "runtime-config.json", "runtime.db-wal", "runtime.db-shm",
            "runtime.sqlite-journal", "client-token." + "a" * 32 + ".new",
            "secrets.enc." + "a" * 32 + ".new", "vault.key." + "a" * 32 + ".new",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(release_check.artifact_reason(Path("copied") / name))
        for name in ("backups/snapshot.json", ".codex/config.toml", "references/instance.local.md"):
            self.assertIsNotNone(release_check.artifact_reason(Path(name)))

    def test_private_content_is_detected_even_in_escaped_skill_json(self):
        personal = chr(92).join(("C:", "Users", "example-user", "workspace"))
        self.assertIn("personal Windows path", release_check.content_issues(personal))
        self.assertIn("personal Windows path", release_check.content_issues(json.dumps({"path": personal})))
        identity = "prj_" + "a" * 32
        self.assertIn("concrete workspace object ID in public skill", release_check.content_issues(identity, skill=True))
        self.assertEqual(release_check.content_issues(identity), [])
        secret = "sk-" + "x" * 40
        self.assertIn("provider credential", release_check.content_issues(secret))
        self.assertNotIn(secret, repr(release_check.content_issues(secret)))
        address = "http://" + ".".join(map(str, (10, 23, 45, 67))) + "/mcp"
        self.assertIn("private service address", release_check.content_issues(address))
        uri = "https://" + "user:password@" + "private-service.test/mcp"
        self.assertIn("credential in service URL", release_check.content_issues(uri))

    def test_public_assets_templates_and_synthetic_examples_remain_allowed(self):
        for name in (
            "docs/images/diagram.svg", "src/ma_runtime/secrets.py",
            "src/ma_runtime/store/schema.sql", ".env.example",
            ".github/workflows/ci.yml", "tests/fixtures/mcp_server.py",
            "integrations/codex/example/references/local-instance.md",
        ):
            with self.subTest(name=name):
                self.assertIsNone(release_check.artifact_reason(Path(name)))
        self.assertEqual(release_check.content_issues(
            "https://user:password@example.invalid/mcp\n"
            "http://127.0.0.1:8890\n"
            "project_id=PROJECT_ID\n"
            "from oea import PlatformClient",
            skill=True,
        ), [])

    def test_fixed_test_data_and_captures_are_not_public_source(self):
        for name in ("tests/fixtures/synthetic.csv", "tests/input.json", "tests/output.jsonl",
                     "tests/fixtures/samples.parquet", "docs/screenshots/overview.png",
                     "tests/captures/recording.webm", "src/.agents/context.md",
                     "AGENTS.md", "PROJECT_SPEC.md", "skills/private/SKILL.md", "mcp/private/mcp.json"):
            with self.subTest(name=name):
                self.assertIsNotNone(release_check.artifact_reason(Path(name)))

    @unittest.skipUnless(shutil.which("git"), "Git is needed to verify tracked/ignored boundaries")
    def test_ignored_files_already_in_the_index_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="oea-index-contract-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            (root / "AGENTS.md").write_text("Local development instructions", encoding="utf-8")
            (root / "skills/private").mkdir(parents=True)
            (root / "skills/private/SKILL.md").write_text("Local integration", encoding="utf-8")
            subprocess.run(["git", "add", "AGENTS.md", "skills/private/SKILL.md"], cwd=root, check=True, capture_output=True)
            (root / ".gitignore").write_text("/AGENTS.md\n/PROJECT_SPEC.md\n/skills/\n/mcp/\n", encoding="utf-8")
            errors = release_check.git_hygiene_issues(root)
            self.assertTrue(any("Tracked AGENTS.md" in error for error in errors))
            self.assertTrue(any("Tracked skills/private/SKILL.md" in error for error in errors))
            (root / ".gitignore").write_text("AGENTS.md\nPROJECT_SPEC.md\n/skills/\n/mcp/\n", encoding="utf-8")
            self.assertTrue(any("root .gitignore rule" in error for error in release_check.git_hygiene_issues(root)))

    def test_source_archive_does_not_require_git(self):
        with tempfile.TemporaryDirectory(prefix="oea-source-contract-") as temp:
            self.assertEqual(release_check.git_hygiene_issues(Path(temp)), [])
