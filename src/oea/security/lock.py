"""OS-scoped workspace exclusion; lock-file existence is not ownership."""

import os
from pathlib import Path

from oea.contracts.errors import RuntimeFault


class WorkspaceLock:
    def __init__(self, root: Path):
        self.path = root / "host.lock"
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if self.path.stat().st_size == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeFault(
                "RUNTIME_ALREADY_OPEN", "Another host owns this data directory"
            ) from None

    def release(self):
        if self.file is not None:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
