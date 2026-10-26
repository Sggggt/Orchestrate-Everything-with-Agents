"""Read-only snapshot boundaries for offline state conversion."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path

from oea.contracts.errors import RuntimeFault, require


_OMITTED = {"runtime.sqlite", "runtime.sqlite-wal", "runtime.sqlite-shm",
            "runtime.sqlite-journal", "host.lock", "runtime.lock"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stopped(root: Path) -> None:
    for name in ("runtime.sqlite-wal", "runtime.sqlite-journal"):
        path = root / name
        require(not path.exists() or path.stat().st_size == 0,
                "MIGRATION_SOURCE_ACTIVE", "Stop the old host and checkpoint SQLite before offline migration")


def _unlock(stream) -> None:
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def offline_source(root: Path):
    """Hold existing host locks without creating or writing source files."""
    with ExitStack() as locks:
        held = set()
        for name in ("host.lock", "runtime.lock"):
            path = root / name
            if not path.exists():
                continue
            stream = locks.enter_context(path.open("rb"))
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBRLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeFault("MIGRATION_SOURCE_ACTIVE", "An active host owns the legacy state directory") from None
            locks.callback(_unlock, stream)
            held.add(name)
        def check():
            _stopped(root)
            require(not any((root / name).exists() for name in {"host.lock", "runtime.lock"} - held),
                    "MIGRATION_SOURCE_CHANGED", "A host lock appeared during offline migration")
        check()
        yield check


def _unreadable(error):
    raise RuntimeFault("MIGRATION_SOURCE_UNREADABLE", "Cannot inventory every legacy state directory") from error


def _material(root: Path):
    for directory, children, files in os.walk(root, followlinks=False, onerror=_unreadable):
        children.sort()
        for child in children:
            path = Path(directory) / child
            require(not path.is_symlink() and not path.is_junction(), "MIGRATION_LINK", "State material contains a directory link")
        for name in sorted(files):
            path = Path(directory) / name
            if path.parent == root and name in _OMITTED:
                continue
            require(not path.is_symlink(), "MIGRATION_LINK", "State material contains a file link")
            yield path


def _stamp(path: Path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ctime_ns


def inventory_files(root: Path) -> list[dict]:
    """Hash state-owned material; external workspaces remain referenced in place."""
    result = []
    for path in _material(root):
        stamp = _stamp(path)
        digest = file_sha256(path)
        require(stamp == _stamp(path), "MIGRATION_SOURCE_CHANGED", "Legacy state material changed while hashing")
        result.append({"relative_path": path.relative_to(root).as_posix(), "sha256": digest,
                       "bytes": stamp[0], "_stamp": stamp})
    return result


def verify_files(root: Path, inventory: list[dict]) -> None:
    remaining = {item["relative_path"]: item["_stamp"] for item in inventory}
    for path in _material(root):
        name = path.relative_to(root).as_posix()
        require(name in remaining and remaining.pop(name) == _stamp(path),
                "MIGRATION_SOURCE_CHANGED", "Legacy state material changed during offline migration")
    require(not remaining, "MIGRATION_SOURCE_CHANGED", "Legacy state material was removed during offline migration")
