"""Include only verified native runtime assets, never an entire build directory."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class NativeBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        configured = os.environ.get("OEA_NATIVE_ASSETS")
        if not configured:
            if version == "editable":
                return  # Source-only development; local native tools fail closed.
            raise ValueError("Set OEA_NATIVE_ASSETS to the staged platform assets from scripts/build_native.py")
        root = Path(configured).resolve()
        if root.is_relative_to(Path(self.root).resolve()):
            raise ValueError("Native build assets must be outside the source checkout")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        expected_os = "windows" if sys.platform == "win32" else "linux" if sys.platform == "linux" else None
        if manifest["os"] != expected_os or manifest["arch"] != "x86_64" or manifest["version"] != self.metadata.version or manifest["protocol"] != 1 or manifest["sdk"] != "1.0.1":
            raise ValueError("Native assets must match the wheel's OS, architecture, version, SDK and protocol")
        entries = [{"path":manifest["binary"],"sha256":manifest["sha256"]}, *manifest["licenses"]]
        if manifest["binary"] != ("oea-sandbox.exe" if expected_os == "windows" else "oea-sandbox") or not manifest["licenses"]:
            raise ValueError("Native assets require the component and redistribution notices")
        include = {str(root / "manifest.json"):"oea/_native/manifest.json"}
        for entry in entries:
            relative = PurePosixPath(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in entry["path"]:
                raise ValueError("Native asset paths must stay inside their staging directory")
            source = (root / relative).resolve()
            if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Native runtime asset failed its manifest check")
            include[str(source)] = "oea/_native/" + relative.as_posix()
        build_data["force_include"].update(include)
        build_data["force_include_editable"] = {**self.build_config.get_force_include(), **include}
        build_data["pure_python"] = False
        build_data["tag"] = "py3-none-" + ("win_amd64" if expected_os == "windows" else "linux_x86_64")
