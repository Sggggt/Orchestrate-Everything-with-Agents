"""Atomic local file replacement without loading credential or OS GUI libraries."""

import os
from pathlib import Path
import uuid


def atomic_write(path: Path, data: bytes):
    temporary = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
