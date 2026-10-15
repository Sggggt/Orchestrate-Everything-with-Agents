"""Content-addressed file storage and checked ArtifactRef reads."""

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path

from .errors import require
from .models import ArtifactRef, View, json_dump, new_id, now_ms
from .paths import safe_child
from .store import emit, one


class ArtifactStore:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = runtime.root / "artifacts"
        self.staging = runtime.root / "staging"
        self.root.mkdir(exist_ok=True)
        self.staging.mkdir(exist_ok=True)

    async def put_file(self, session_id: str, path: str | Path, *, metadata=None):
        source = Path(path).resolve(strict=True)
        require(
            source.is_file(),
            "ARTIFACT_INVALID",
            "Artifact source must be a regular file",
        )
        return await self._put(
            session_id, source.name, source=source, metadata=metadata
        )

    async def put_bytes(
        self, session_id: str, name: str, data: bytes, *, metadata=None
    ):
        return await self._put(session_id, name, data=data, metadata=metadata)

    async def _put(self, session_id, name, *, source=None, data=None, metadata=None):
        session = await self.runtime.sessions.get(session_id)
        require(session.state != "DELETED", "SESSION_DELETED", "Restore the session before adding artifacts")
        name = (
            re.sub(r"[^\w.\-]", "_", Path(name.replace("\\", "/")).name)[:120].strip(
                ". "
            )
            or "artifact"
        )
        devices = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if name.split(".")[0].upper() in devices:
            name = "_" + name
        stage = safe_child(self.staging, new_id("stg"))

        def copy():
            digest = hashlib.sha256()
            size = 0
            try:
                with stage.open("xb") as output:
                    if source is None:
                        chunks = (data,)
                        input_stream = None
                    else:
                        input_stream = source.open("rb")
                        chunks = iter(lambda: input_stream.read(1024 * 1024), b"")
                    try:
                        for chunk in chunks:
                            size += len(chunk)
                            require(
                                size <= self.runtime.config.max_artifact_bytes,
                                "ARTIFACT_INVALID",
                                "Artifact exceeds the configured size limit",
                            )
                            digest.update(chunk)
                            output.write(chunk)
                    finally:
                        if input_stream is not None:
                            input_stream.close()
                    output.flush()
                    os.fsync(output.fileno())
                checksum = digest.hexdigest()
                relative = f"sha256/{checksum[:2]}/{checksum}/{name}"
                target = safe_child(self.root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                safe_child(self.root, relative)
                if target.exists():
                    require(
                        hashlib.sha256(target.read_bytes()).hexdigest() == checksum,
                        "ARTIFACT_INVALID",
                        "Existing content-addressed file is corrupt",
                    )
                    stage.unlink()
                else:
                    os.replace(stage, target)
                return checksum, size, relative
            finally:
                if stage.exists():
                    stage.unlink()

        checksum, size, relative = await asyncio.to_thread(copy)
        artifact_id = new_id("art")
        meta = self.runtime.vault.redact(metadata or {})

        def insert(db):
            session = one(db, "SELECT state FROM sessions WHERE id=?", (session_id,))
            require(session is not None and session.state != "DELETED", "SESSION_DELETED",
                    "Restore the session before adding artifacts")
            db.execute(
                "INSERT INTO artifacts(id,session_id,sha256,size,relative_path,name,media_type,state,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    session_id,
                    checksum,
                    size,
                    relative,
                    name,
                    mimetypes.guess_type(name)[0] or "application/octet-stream",
                    "READY",
                    json_dump(meta),
                    now_ms(),
                ),
            )
            emit(
                db,
                "artifact.created",
                session_id=session_id,
                payload={"artifact_id": artifact_id, "name": name},
            )

        await self.runtime.store.call(insert, transaction=True)
        self.runtime.notify()
        return await self.get(artifact_id)

    async def get(self, artifact_id):
        found = await self.runtime.store.call(
            lambda db: one(db, "SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        )
        require(found is not None, "NOT_FOUND", "Artifact does not exist")
        result = View(found)
        result["schema_version"] = "1.0"
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result.pop("relative_path")
        return ArtifactRef.model_validate(result)

    async def open(self, artifact_id):
        found = await self.runtime.store.call(
            lambda db: one(db, "SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        )
        require(
            found is not None and found.state == "READY",
            "ARTIFACT_INVALID",
            "Artifact is unavailable",
        )
        path = safe_child(self.root, found.relative_path)

        def verify():
            require(
                path.is_file() and path.stat().st_size == found.size,
                "ARTIFACT_INVALID",
                "Artifact is missing or has changed",
            )
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            require(
                digest.hexdigest() == found.sha256,
                "ARTIFACT_INVALID",
                "Artifact checksum mismatch",
            )

        await asyncio.to_thread(verify)
        return path
