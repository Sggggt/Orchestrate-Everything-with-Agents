"""All mutable state belongs outside source repositories."""

import os
from pathlib import Path

from .errors import require


def require_local_state(path: Path):
    if os.name == "nt":
        import ctypes

        require(
            not str(path).startswith("\\\\")
            and ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 4,
            "INVALID_DATA_DIRECTORY",
            "SQLite state must be stored on a local disk",
        )
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        directory = os.environ.get(name)
        if directory:
            require(
                not path.is_relative_to(Path(directory).resolve()),
                "INVALID_DATA_DIRECTORY",
                "SQLite state must be stored outside synchronized directories",
            )


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        current, legacy = base / "OEA/data", base / "MultiAgentPlatform/data"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        current, legacy = base / "oea", base / "local-multi-agent"
    if not (current / "runtime.sqlite").exists() and (legacy / "runtime.sqlite").exists():
        return legacy
    return current


def external_path(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    for ancestor in (target, *target.parents):
        require(
            not (ancestor / ".git").exists(),
            "INVALID_DATA_DIRECTORY",
            "Mutable data must be stored outside Git repositories",
        )
    source = Path(__file__).resolve()
    for ancestor in source.parents:
        if (ancestor / "pyproject.toml").is_file() and (ancestor / "agent").is_dir():
            require(
                not target.is_relative_to(ancestor),
                "INVALID_DATA_DIRECTORY",
                "Mutable data must be stored outside the source directory",
            )
            break
    return target


def safe_child(root: Path, relative: str) -> Path:
    for part in (root, *root.parents):
        require(
            not part.is_symlink() and not part.is_junction(),
            "ARTIFACT_INVALID",
            "Links are not permitted in managed directory ancestry",
        )
    target = (root / relative).resolve()
    require(
        target.is_relative_to(root.resolve()) and target != root.resolve(),
        "ARTIFACT_INVALID",
        "Path is outside the managed directory",
    )
    part = root / relative
    while part != root and part != part.parent:
        require(
            not part.is_symlink() and not part.is_junction(),
            "ARTIFACT_INVALID",
            "Links are not permitted in managed paths",
        )
        part = part.parent
    return target
