"""Windows launchers start and reuse only isolated test hosts."""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Requires Windows PowerShell")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-launcher-")
        self.root = Path(self.temporary.name)
        self.environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                            "PYTHONPATH": str(ROOT / "src"), "OEA_HOME": str(self.root / "home"), "OEA_PYTHON": sys.executable,
                            "OEA_DATA_DIR": str(self.root / "configured-state"),
                            "LOCALAPPDATA": str(self.root / "local-app-data")}
        self.host_pids = set()
        self.launch_count = 0

    def processes(self):
        # The random test root is an ownership marker; never return other hosts.
        command = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
                   "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
                   "Where-Object { $_.CommandLine -and $_.CommandLine.Contains(" + ps_string(self.root) + ") } | "
                   "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return []
        values = json.loads(result.stdout)
        return values if isinstance(values, list) else [values]

    def stop_hosts(self):
        for process in self.processes():
            command = process["CommandLine"]
            self.assertIn(str(self.root), command)
            self.assertIn("--data-dir", command)
            self.assertIn("oea", command)
            self.assertIn("serve", command)
            # This is exclusively the tree whose command names our random state.
            subprocess.run(["taskkill.exe", "/PID", str(process["ProcessId"]), "/T", "/F"],
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(self.processes(), [], "A test-owned host survived cleanup")

    def tearDown(self):
        try:
            self.stop_hosts()
        finally:
            self.temporary.cleanup()

    @staticmethod
    def free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def launch(self, data_dir=None, *, port=None, python=None, command=False, environment=None):
        python = str(python or sys.executable)
        options = ["-Python", python, "-NoBrowser"]
        if data_dir is not None:
            options.extend(["-DataDir", str(data_dir)])
        if port is not None:
            options.extend(["-Port", str(port)])
        if command:
            script = subprocess.list2cmdline([str(ROOT / "Start-Workspace.cmd"), *options])
            arguments = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")]) + ' /d /s /c "' + script + '"'
        else:
            script = "& " + ps_string(ROOT / "Start-Workspace.ps1")
            for option in options:
                script += " " + (option if option.startswith("-") else ps_string(option))
            arguments = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
        self.launch_count += 1
        output = self.root / ("launch-" + str(self.launch_count) + ".out.log")
        error = self.root / ("launch-" + str(self.launch_count) + ".err.log")
        # Background Windows processes can inherit pipe handles. Files allow the
        # launcher to exit independently while retaining its startup diagnostics.
        with output.open("wb") as stdout, error.open("wb") as stderr:
            result = subprocess.run(arguments, stdout=stdout, stderr=stderr, timeout=65,
                                    env=environment or self.environment, cwd=self.root,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        result.stdout = output.read_text(encoding="utf-8", errors="replace")
        result.stderr = error.read_text(encoding="utf-8", errors="replace")
        self.host_pids.update(int(value) for value in re.findall(r"Host PID:\s+(\d+)", result.stdout))
        return result

    def assert_host(self, state, port):
        metadata = json.loads((state / "client.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["url"], "http://127.0.0.1:" + str(port))
        request = urllib.request.Request(metadata["url"] + "/api/protocol", headers={
            "Authorization": "Bearer " + (state / "client-token").read_text(encoding="utf-8").strip()})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            protocol = json.load(response)
        self.assertEqual(protocol["protocol_version"], "2.0")
        self.assertEqual(protocol["engine"], "langgraph")

    def startup_diagnostics(self, result):
        return result.stdout + result.stderr + "\n".join(
            log.read_text(encoding="utf-8", errors="replace")
            for log in self.root.rglob("launcher-*.err.log"))

    def test_explicit_empty_data_directory_uses_default_and_ignores_configured_path(self):
        port = self.free_port()
        environment = {**self.environment, "OEA_PYTHON": str(self.root / "unavailable-python.exe")}
        started = self.launch("", port=port, environment=environment)
        self.assertEqual(started.returncode, 0, self.startup_diagnostics(started))
        state = Path(environment["OEA_HOME"]) / "data"
        self.assert_host(state, port)
        self.assertFalse(Path(environment["OEA_DATA_DIR"]).exists())
        self.assertNotIn("IndexError", started.stdout + started.stderr)

    def test_no_data_argument_uses_standard_home_despite_old_user_environment(self):
        port = self.free_port()
        started = self.launch(port=port)
        self.assertEqual(started.returncode, 0, self.startup_diagnostics(started))
        self.assert_host(Path(self.environment["OEA_HOME"]) / "data", port)
        self.assertFalse(Path(self.environment["OEA_DATA_DIR"]).exists())
        self.assertFalse(Path(self.environment["LOCALAPPDATA"]).exists())

    def test_cmd_start_reuse_port_conflict_and_offline_restart_keep_one_state_owner(self):
        state = self.root / "state with spaces \u8def\u5f84"
        port = self.free_port()
        started = self.launch(state, port=port, command=True)
        self.assertEqual(started.returncode, 0, self.startup_diagnostics(started))
        self.assertTrue(self.host_pids)
        self.assert_host(state, port)
        token = (state / "client-token").read_bytes()
        logs = tuple((state / "logs").glob("launcher-*.err.log"))
        reused = self.launch(state)
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertNotIn("Host PID:", reused.stdout)
        self.assertEqual(tuple((state / "logs").glob("launcher-*.err.log")), logs)
        changed = self.launch(state, port=self.free_port())
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("already served", changed.stdout + changed.stderr)
        self.assert_host(state, port)
        self.stop_hosts()
        restarted = self.launch(state)
        self.assertEqual(restarted.returncode, 0, self.startup_diagnostics(restarted))
        self.assertIn("Host PID:", restarted.stdout)
        self.assert_host(state, port)
        self.assertEqual((state / "client-token").read_bytes(), token)
        explicit_port = self.free_port()
        self.stop_hosts()
        explicit = self.launch(state, port=explicit_port)
        self.assertEqual(explicit.returncode, 0, self.startup_diagnostics(explicit))
        self.assert_host(state, explicit_port)
        self.assertEqual((state / "client-token").read_bytes(), token)

    def test_busy_port_fails_before_creating_host_state(self):
        state = self.root / "blocked-state"
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result = self.launch(state, port=occupied.getsockname()[1])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is in use", result.stdout + result.stderr)
        self.assertFalse(state.exists())
        self.assertEqual(self.processes(), [])

    def test_missing_installation_names_selected_python_without_starting_host(self):
        isolated = self.root / "uninstalled-python"
        subprocess.run([sys.executable, "-B", "-m", "venv", "--without-pip", str(isolated)],
                       check=True, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        python = isolated / "Scripts" / "python.exe"
        environment = {**self.environment, "PYTHONPATH": ""}
        result = self.launch(self.root / "unopened-state", python=python, environment=environment)
        self.assertNotEqual(result.returncode, 0)
        message = result.stdout + result.stderr
        self.assertIn(str(python), message)
        self.assertIn("not installed", message)
        self.assertIn("pip install", message)
        self.assertNotIn("IndexError", message)
        self.assertNotIn("Traceback", message)
        self.assertFalse((self.root / "unopened-state").exists())
