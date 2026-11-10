"""Install a release wheel in a new external environment and exercise its APIs."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import venv


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--native", action="store_true", help="Also exercise the actual OS sandbox backend")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    if not args.work_dir.is_absolute() or work.is_relative_to(ROOT) or work.exists():
        raise ValueError("Choose a new absolute installation-check directory outside the checkout")
    work.mkdir(parents=True)
    temporary = work / "temp"
    temporary.mkdir()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("OEA_TEST_NATIVE_COMPONENT", None)
    environment.pop("OEA_TEST_NATIVE_NETWORK_ALLOW", None)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", OEA_HOME=str(work / "home"),
                       PIP_CACHE_DIR=str(work / "pip-cache"), TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
    runtime = work / "venv"
    venv.EnvBuilder(with_pip=True).create(runtime)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def run(*arguments):
        subprocess.run([str(python), "-B", *map(str, arguments)], cwd=work, env=environment, check=True)

    run("-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "-c", ROOT / "constraints.txt",
        str(args.wheel.resolve()) + "[web,mcp,workbench]")
    run("-m", "pip", "check")
    run("-m", "oea", "--version")
    inspect = """import hashlib,json,subprocess
from pathlib import Path
from importlib.metadata import version
import oea
from oea.workspace.native import executable
binary=executable()
manifest=json.loads(binary.with_name('manifest.json').read_text(encoding='utf-8'))
assert hashlib.sha256(binary.read_bytes()).hexdigest()==manifest['sha256']
print(json.dumps({'version':oea.__version__,'metadata_version':version('orchestrate-everything-with-agents'),
 'package':str(Path(oea.__file__).parent),'binary':str(binary),
 'native':json.loads(subprocess.check_output([str(binary),'--version'],text=True,encoding='utf-8'))}))
"""
    installed = json.loads(subprocess.check_output([str(python), "-B", "-c", inspect], cwd=work, env=environment,
                                                   text=True, encoding="utf-8"))
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not Path(installed["package"]).is_relative_to(runtime) or not Path(installed["binary"]).is_relative_to(Path(installed["package"]) / "_native"):
        raise RuntimeError("Installation checks must use only the wheel's installed package and native component")
    if installed["version"] != expected or installed["metadata_version"] != expected or installed["native"]["version"] != expected:
        raise RuntimeError("Installed Python, metadata and native versions must match the release")
    environment["PYTHONPATH"] = str(ROOT / "tests")
    cases = ["test_release", "test_sdk", "test_entrypoints", "test_http"]
    if args.native:
        environment["OEA_TEST_NATIVE_COMPONENT"] = installed["binary"]
        environment["OEA_TEST_NATIVE_NETWORK_ALLOW"] = "1"
        cases.append("test_native_sandbox")
    run("-m", "unittest", *cases, "-v")
    print(f"Installed wheel checks passed: {expected}; native execution requested: {args.native}")


if __name__ == "__main__":
    main()
