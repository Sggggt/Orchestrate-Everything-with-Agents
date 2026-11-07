"""Default entry points share an application-owned home without creating state."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from oea.contracts.errors import RuntimeFault
from oea.interfaces.cli import main, parser
from oea.interfaces.client import PlatformClient
from oea.interfaces.launch import serve
from oea.runtime.host import Host
from oea.workspace.paths import default_data_dir, installation_paths, resolve_data_dir, user_home


class UserHomeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-home-")
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"OEA_HOME": str(self.root / "home")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_default_home_ignores_callers_app_cache_old_overrides_and_working_directory(self):
        with patch.dict(os.environ, {"OEA_HOME": "", "LOCALAPPDATA": str(self.root / "caller-cache"),
                                    "XDG_STATE_HOME": str(self.root / "caller-state"),
                                    "OEA_DATA_DIR": str(self.root / "old-state")}), patch("pathlib.Path.home", return_value=self.root / "user"):
            self.assertEqual(user_home(), self.root / "user/.oea")
            self.assertEqual(default_data_dir(), self.root / "user/.oea/data")
        self.assertFalse((self.root / "user").exists())

    def test_all_default_entrypoints_resolve_the_same_home_and_keep_explicit_isolation(self):
        expected = self.root / "home/data"
        self.assertEqual(Host().root, expected)
        self.assertEqual(resolve_data_dir(), expected)
        self.assertEqual(resolve_data_dir(""), expected)
        for args in (["serve"], ["mcp"], ["request", "host.status"]):
            self.assertIsNone(parser().parse_args(args).data_dir)
        self.assertIsNone(parser().parse_args(["serve"]).port)
        self.assertEqual(resolve_data_dir(self.root / "isolated"), self.root / "isolated")
        with self.assertRaises(RuntimeFault) as missing:
            PlatformClient.from_data_dir()
        self.assertEqual(missing.exception.code, "HOST_NOT_CONFIGURED")
        self.assertFalse(expected.exists(), "A client must not create another host's state")

    def test_home_override_must_be_absolute_and_state_remains_outside_git(self):
        with patch.dict(os.environ, {"OEA_HOME": "relative-home"}), self.assertRaises(RuntimeFault):
            user_home()
        repository = self.root / "repository"
        (repository / ".git").mkdir(parents=True)
        with self.assertRaises(RuntimeFault):
            resolve_data_dir(repository / "data")

    def test_paths_command_reports_the_layout_without_writes(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["paths"]), 0)
        paths = json.loads(output.getvalue())
        self.assertEqual(paths, installation_paths())
        self.assertEqual(set(paths), {"home", "data", "runtime", "cache", "bin"})
        self.assertFalse((self.root / "home").exists())

    def test_known_old_data_does_not_silently_become_an_empty_default_workspace(self):
        old = self.root / "old-state"
        old.mkdir()
        (old / "oea.sqlite").touch()
        with patch.dict(os.environ, {"OEA_HOME": "", "OEA_DATA_DIR": str(old)}), patch("pathlib.Path.home", return_value=self.root / "user"):
            with self.assertRaises(RuntimeFault) as migration:
                Host()
            self.assertEqual(migration.exception.code, "DATA_RELOCATION_REQUIRED")
            self.assertEqual(resolve_data_dir(old), old)
            self.assertFalse((self.root / "user/.oea/data").exists())

    def metadata(self, url="http://127.0.0.1:8897"):
        state = default_data_dir()
        state.mkdir(parents=True)
        (state / "client.json").write_text(json.dumps({"url": url}), encoding="utf-8")
        (state / "client-token").write_text("test-token", encoding="utf-8")
        return state

    def test_existing_host_is_reused_and_different_ports_are_not_silently_ignored(self):
        state = self.metadata()
        with patch.object(PlatformClient, "__aenter__", new_callable=AsyncMock), \
                patch.object(PlatformClient, "__aexit__", new_callable=AsyncMock), \
                patch.object(PlatformClient, "call", new_callable=AsyncMock, return_value={"data_dir": str(state)}), \
                patch("uvicorn.run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            serve()
            self.assertIn("http://127.0.0.1:8897", output.getvalue())
            run.assert_not_called()
            with self.assertRaises(RuntimeFault) as conflict:
                serve(port=8898)
            self.assertEqual(conflict.exception.code, "HOST_ALREADY_RUNNING")

    def test_an_offline_host_restarts_on_its_saved_port_but_timeouts_do_not_start_another(self):
        state = self.metadata()
        with patch.object(PlatformClient, "__aenter__", new_callable=AsyncMock, side_effect=httpx.ConnectError("offline")), \
                patch("oea.web.app.create_app", return_value="app") as create, patch("uvicorn.run") as run:
            serve()
            self.assertEqual(run.call_args.kwargs["port"], 8897)
            self.assertEqual(create.call_args.args[0], state)
        with patch.object(PlatformClient, "__aenter__", new_callable=AsyncMock, side_effect=httpx.ReadTimeout("unknown")), patch("uvicorn.run") as run:
            with self.assertRaises(httpx.ReadTimeout):
                serve()
            run.assert_not_called()

    def test_default_client_reads_the_same_saved_endpoint_and_rejects_invalid_metadata(self):
        state = self.metadata()
        self.assertEqual(PlatformClient.from_data_dir().url, "http://127.0.0.1:8897")
        (state / "client.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeFault) as invalid:
            PlatformClient.from_data_dir()
        self.assertEqual(invalid.exception.code, "INVALID_HOST_METADATA")
