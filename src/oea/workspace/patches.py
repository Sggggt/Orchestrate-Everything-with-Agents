"""Bounded context patches with compare-and-write ownership."""

import asyncio
import hashlib
import os
import stat
import uuid
from oea.contracts.errors import require
from oea.security.files import atomic_write

PATCH_MAX_BYTES = 8 * 1024 * 1024


class FileWrites:
    """Serialize only a shared file's compare-and-write boundary."""
    def __init__(self):
        self.entries = {}

    async def run(self, path, write, *, asynchronous=False):
        key = os.path.normcase(str(path))
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = {"lock": asyncio.Lock(), "users": 0}
        entry["users"] += 1
        async def execute():
            async with entry["lock"]:
                return await write() if asynchronous else await asyncio.to_thread(write)
        task = asyncio.create_task(execute())
        def finished(completed):
            # The worker retains ownership even if its consumer leaves.
            entry["users"] -= 1
            if not entry["users"]:
                self.entries.pop(key, None)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)


def patch_bytes(arguments, previous):
    operation, expected = arguments["operation"], arguments.get("expected_sha256")
    if expected is not None:
        require(previous is not None and hashlib.sha256(previous).hexdigest() == expected,
                "REVISION_CONFLICT", "The file changed since the expected revision")
    if operation == "add":
        require(previous is None, "PATCH_CONFLICT", "Add requires an absent file")
        return arguments["content"].encode("utf-8")
    if operation == "delete":
        require(previous is not None and expected is not None, "PATCH_CONFLICT", "Delete requires the current sha256")
        return None
    require(previous is not None, "PATCH_CONFLICT", "Update requires an existing file")
    text = previous.decode("utf-8")
    for hunk in arguments["hunks"]:
        old = hunk["old_text"]
        require(old and text.count(old) == 1, "PATCH_CONFLICT", "Patch context must match exactly once")
        text = text.replace(old, hunk["new_text"], 1)
    return text.encode("utf-8")


def apply_patch(arguments, paths):
    """Resolve once, then access relative to held directory handles on isolated hosts."""
    with paths.parent(arguments["path"], write=True, create=arguments["operation"] == "add") as (path, parent):
        if parent is None:
            if path.exists():
                with path.open("rb") as source:
                    previous = source.read(PATCH_MAX_BYTES + 1)
            else:
                previous = None
            require(previous is None or len(previous) <= PATCH_MAX_BYTES, "PATCH_TOO_LARGE", "Use a program to edit files larger than 8 MiB")
            data = patch_bytes(arguments, previous)
            if data is None:
                path.unlink()
            else:
                atomic_write(path, data)
        else:
            try:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                previous = None
            else:
                with os.fdopen(descriptor, "rb") as source:
                    info = os.fstat(source.fileno())
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "PERMISSION_DENIED", "Patch requires an unlinked regular file")
                    previous = source.read(PATCH_MAX_BYTES + 1)
                    require(len(previous) <= PATCH_MAX_BYTES, "PATCH_TOO_LARGE", "Use a program to edit files larger than 8 MiB")
            data = patch_bytes(arguments, previous)
            if data is None:
                os.unlink(path.name, dir_fd=parent)
            else:
                temporary = ".oea-patch-" + uuid.uuid4().hex
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        if previous is not None:
                            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
    if data is None:
        return {"path": str(path), "deleted": True}
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "artifact_candidate": True}
