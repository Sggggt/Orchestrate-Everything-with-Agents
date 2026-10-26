"""All mutable state belongs outside source repositories."""

import os
from pathlib import Path

from oea.contracts.errors import require


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


def user_home() -> Path:
    """One application-owned root, independent of the caller's cache and cwd."""
    override = os.environ.get("OEA_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".oea"
    require(root.is_absolute(), "INVALID_DATA_DIRECTORY", "OEA_HOME must be an absolute path")
    return root.resolve()


def default_data_dir() -> Path:
    return user_home() / "data"


def resolve_data_dir(path: str | Path | None = None) -> Path:
    target = external_path(path if path is not None and str(path) else default_data_dir())
    require_local_state(target)
    # A known existing installation must not silently become an empty workspace.
    # This detects the retired setting only; it never selects or opens that state.
    previous = os.environ.get("OEA_DATA_DIR")
    if not path and not os.environ.get("OEA_HOME") and previous and not (target / "oea.sqlite").exists():
        old = Path(previous).expanduser().resolve()
        require(old == target or not (old / "oea.sqlite").exists(), "DATA_RELOCATION_REQUIRED",
                "Existing data uses the retired OEA_DATA_DIR setting. Relocate it offline before starting the default home")
    return target


def installation_paths() -> dict[str, str]:
    """Describe the managed layout without creating state or starting a host."""
    root = user_home()
    return {"home": str(root), **{name: str(root / name) for name in ("data", "runtime", "cache", "bin")}}


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
        if (ancestor / "pyproject.toml").is_file() and (ancestor / "src/oea").is_dir():
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
