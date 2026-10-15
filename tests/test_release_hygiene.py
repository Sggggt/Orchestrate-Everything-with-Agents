"""Release checks reject copied private state without hiding useful source assets."""

import importlib.util
import json
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
            "docs/images/overview.png", "src/ma_runtime/secrets.py",
            "src/ma_runtime/store/schema.sql", ".env.example",
            ".github/workflows/ci.yml", "tests/fixtures/synthetic.csv",
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
