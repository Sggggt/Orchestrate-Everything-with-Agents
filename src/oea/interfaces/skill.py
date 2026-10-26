"""Export the distributable Codex skill without changing existing installations."""

from importlib import resources
from pathlib import Path
import shutil

from oea.contracts.errors import require
from oea.workspace.paths import external_path


def export_skill(output: Path):
    """Copy the complete bundled skill to a new directory outside source trees."""
    target = external_path(output)
    require(not target.exists(), "TARGET_EXISTS", "Choose a new skill directory; existing files are preserved")
    bundled = resources.files("oea").joinpath("codex_skill")
    if not bundled.joinpath("SKILL.md").is_file():
        bundled = Path(__file__).resolve().parents[3] / "integrations/codex/orchestrate-everything-with-agents"
    require(bundled.joinpath("SKILL.md").is_file(), "SKILL_UNAVAILABLE", "The installed distribution has no bundled Codex skill")
    with resources.as_file(bundled) as source:
        shutil.copytree(source, target)
    return {"path": str(target), "protocol_version": "2.0"}
