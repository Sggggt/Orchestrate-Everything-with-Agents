"""Public entry points reject retired contracts and keep inspection side-effect free."""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea.interfaces.cli import main, parser

ROOT = Path(__file__).resolve().parents[1]


class ReleaseV2Tests(unittest.TestCase):
    def test_only_oea_namespace_is_installed_and_imports_start_no_threads(self):
        code = "import threading,json,importlib.util; before={t.ident for t in threading.enumerate()}; import oea; print(json.dumps({'new_threads':len({t.ident for t in threading.enumerate()}-before),'retired':{name:importlib.util.find_spec(name) is not None for name in ('ma_runtime','agent')}}))"
        value = json.loads(subprocess.check_output([sys.executable, "-B", "-c", code], text=True))
        self.assertEqual(value, {"new_threads": 0, "retired": {"ma_runtime": False, "agent": False}})

    def test_public_source_has_no_retired_runtime_import(self):
        for path in (ROOT / "src/oea").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    package = ".".join(path.relative_to(ROOT / "src").parts[:-1])
                    modules = [importlib.util.resolve_name("." * node.level + (node.module or ""), package)] if node.level else [node.module]
                else:
                    modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
                self.assertFalse(any(module and module.split(".")[0] in {"ma_runtime", "agent"} for module in modules), str(path.relative_to(ROOT)))

    def test_cli_rejects_retired_switches_and_defaults_to_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (["serve", "--data-dir", "example", "--mock"], ["mcp", "--connect"], ["serve", "--data-dir", "example", "--reserved-external-slots", "1"]):
                with self.assertRaises(SystemExit):
                    parser().parse_args(args)
        args = parser().parse_args(["migrate", "--source", "old-state"])
        self.assertFalse(args.apply)
        self.assertIsNone(args.target)

    def test_migration_cli_calls_offline_service_without_starting_host(self):
        with patch("oea.migration.offline.migrate_legacy", return_value={"mode": "dry-run"}) as migrate, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["migrate", "--source", "old-state"]), 0)
        migrate.assert_called_once_with(Path("old-state"), target=None, apply=False)
        self.assertEqual(json.loads(output.getvalue()), {"mode": "dry-run"})

    def test_schema_export_requires_no_host(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["schema", "agent"]), 0)
        self.assertIn("properties", json.loads(output.getvalue()))

    def test_bundled_skill_exports_complete_tree_without_overwriting_local_edits(self):
        from oea.contracts.errors import RuntimeFault
        from oea.interfaces.skill import export_skill
        with tempfile.TemporaryDirectory(prefix="oea-skill-export-") as temporary:
            target = Path(temporary) / "skill"
            exported = export_skill(target)
            self.assertEqual(exported["protocol_version"], "2.0")
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "workflows/develop.md").is_file())
            self.assertTrue((target / "assets/mark.svg").is_file())
            (target / "SKILL.md").write_text("Local instructions", encoding="utf-8")
            with self.assertRaises(RuntimeFault) as exists:
                export_skill(target)
            self.assertEqual(exists.exception.code, "TARGET_EXISTS")
            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "Local instructions")
