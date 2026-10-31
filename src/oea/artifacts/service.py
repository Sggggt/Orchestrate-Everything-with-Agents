"""Attributed artifact publication and consumer-owned streaming uploads."""

import asyncio
import codecs
import hashlib
import mimetypes
import os
from pathlib import Path
import re

from oea.contracts.domain import Artifact
from oea.contracts.measurement import content_view
from oea.contracts.errors import RuntimeFault, require
from oea.workspace.paths import safe_child
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows
from oea.interfaces.registry import operation_context


async def file_io(function, *args):
    """Finish an owned file operation before cancellation closes its handle."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class Artifacts:
    def __init__(self, host):
        self.host = host
        self.receiving = set()
        self.receiving_projects = {}

    async def publish(self, run_id, source, name, *, media_type=None, expected_sha256=None):
        context = self.host.active.get(run_id)
        require(context is not None, "STALE_RUN", "Run no longer owns this publication")
        paths, _ = await self.host.resolve_workspace(run_id)
        source = paths.resolve(str(source))
        require(name and Path(name).name == name and name not in {".", ".."}, "ARTIFACT_INVALID", "Artifact name cannot contain directories")
        identity = new_id("artifact")
        directory = self.host.root / "artifacts"
        directory.mkdir(exist_ok=True)
        destination = safe_child(directory, identity)

        def copy():
            fingerprint, size = hashlib.sha256(), 0
            with source.open("rb") as original, destination.open("xb") as output:
                while chunk := original.read(1024 * 1024):
                    size += len(chunk)
                    require(size <= 50 * 1024 * 1024, "ARTIFACT_TOO_LARGE", "Artifact exceeds the publication size limit")
                    fingerprint.update(chunk)
                    output.write(chunk)
            return size, fingerprint.hexdigest()

        size, fingerprint = await asyncio.to_thread(copy)
        require(expected_sha256 is None or expected_sha256 == fingerprint, "ARTIFACT_CHANGED", "Source changed after image validation")
        media = media_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        artifact = Artifact(id=identity, name=name, sha256=fingerprint, bytes=size, media_type=media,
                            project_id=context.instance["project_id"], session_id=context.instance["session_id"], run_id=run_id, instance_id=context.instance["id"])
        def write(db):
            run = one(db, "SELECT state FROM runs WHERE id=?", (run_id,))
            require(run and run["state"] == "running", "STALE_RUN", "Run no longer owns this publication")
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,'run')",
                       (identity, context.instance["project_id"], context.instance["id"], run_id, name, str(destination), fingerprint, size, media, now_ms(), context.instance["session_id"]))
            event(db, "artifact.published", project_id=context.instance["project_id"], session_id=context.instance["session_id"],
                  instance_id=context.instance["id"], run_id=run_id, data=artifact.model_dump(mode="json"))
        await self.host.store.call(write, transaction=True)
        self.host.notify()
        return artifact.model_dump(mode="json")

    @staticmethod
    def consumer(consumer_id):
        context = operation_context.get() or {}
        reader = consumer_id or context.get("consumer_id")
        require(isinstance(reader, str) and 0 < len(reader) <= 256 and (not context.get("consumer_id") or reader == context["consumer_id"]),
                "PERMISSION_DENIED", "Use one stable controller consumer identity")
        return reader

    async def begin_upload(self, project_id: str, name: str, bytes: int, sha256: str, idempotency_key: str, *,
                           session_id: str | None = None, consumer_id: str | None = None):
        """Accept upload metadata once; binary content is streamed separately."""
        self.host.assert_open()
        consumer = self.consumer(consumer_id)
        require(0 < len(name) <= 240 and Path(name).name == name and name not in {".", ".."} and not any(char in name for char in "\x00\r\n/\\"),
                "ARTIFACT_INVALID", "Use a file name without directories or control characters")
        require(0 <= bytes <= 50 * 1024 * 1024 and re.fullmatch("[a-f0-9]{64}", sha256) and 0 < len(idempotency_key) <= 256,
                "INVALID_REQUEST", "Supply a SHA-256, a stable key and a byte count within 50 MiB")
        fingerprint = hashlib.sha256(encode([name, bytes, sha256, session_id]).encode("utf-8")).hexdigest()
        def accept(db):
            project = one(db, "SELECT archived FROM projects WHERE id=?", (project_id,))
            require(project is not None, "NOT_FOUND", "Project does not exist")
            prior = one(db, "SELECT * FROM artifact_uploads WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (project_id, consumer, idempotency_key))
            if prior:
                require(prior["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Upload key has different metadata")
                return prior["id"]
            if session_id:
                session = one(db, "SELECT project_id FROM sessions WHERE id=?", (session_id,))
                require(session and session["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            identity = new_id("artifact")
            media = mimetypes.guess_type(name)[0] or "application/octet-stream"
            db.execute("INSERT INTO artifact_uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, project_id, session_id, consumer, idempotency_key, fingerprint, name, bytes, sha256, media, now_ms()))
            event(db, "artifact.upload.accepted", project_id=project_id, session_id=session_id, data={"artifact_id": identity})
            return identity
        identity = await self.host.store.call(accept, transaction=True)
        self.host.notify()
        return await self.get_upload(identity, project_id, consumer_id=consumer)

    async def get_upload(self, upload_id: str, project_id: str, *, consumer_id: str | None = None):
        """Read the original upload receipt without repeating file publication."""
        consumer = self.consumer(consumer_id)
        def read(db):
            record = one(db, "SELECT u.*,a.id AS artifact_id FROM artifact_uploads u LEFT JOIN artifacts a ON a.id=u.id WHERE u.id=? AND u.project_id=? AND u.consumer_id=?", (upload_id, project_id, consumer))
            require(record is not None, "PERMISSION_DENIED", "Upload belongs to another consumer or project")
            return {key: record[key] for key in ("id", "project_id", "session_id", "name", "bytes", "sha256", "media_type")} | {
                "state": "completed" if record["artifact_id"] else "accepted", "artifact_id": record["artifact_id"]}
        return await self.host.store.call(read)

    async def _commit_upload(self, record, path, consumer):
        def save(db):
            prior = one(db, "SELECT * FROM artifacts WHERE id=?", (record["id"],))
            if prior:
                return
            db.execute("INSERT INTO artifacts VALUES(?,?,NULL,NULL,?,?,?,?,?,?,?,'upload')", (record["id"], record["project_id"], record["name"], str(path), record["sha256"], record["bytes"], record["media_type"], now_ms(), record["session_id"]))
            event(db, "artifact.published", project_id=record["project_id"], session_id=record["session_id"], data={"artifact_id": record["id"], "origin": "upload"})
        await self.host.store.call(save, transaction=True)
        self.host.notify()
        return await self.get_upload(record["id"], record["project_id"], consumer_id=consumer)

    async def write_upload(self, upload_id, project_id, chunks, *, consumer_id):
        """Receive authenticated bounded chunks; only validated bytes become visible."""
        self.host.assert_open()
        self.host.assert_project_available(project_id)
        task = asyncio.current_task()
        self.receiving.add(task)
        self.receiving_projects[task] = project_id
        try:
            return await self._receive(upload_id, project_id, chunks, consumer_id)
        finally:
            self.receiving.discard(task)
            self.receiving_projects.pop(task, None)

    async def _receive(self, upload_id, project_id, chunks, consumer_id):
        consumer = self.consumer(consumer_id)
        record = await self.get_upload(upload_id, project_id, consumer_id=consumer)
        if record["state"] == "completed":
            return record
        directory = self.host.root / "artifacts"
        directory.mkdir(exist_ok=True)
        destination = safe_child(directory, upload_id)
        temporary = safe_child(directory, new_id("upload-part"))
        size, fingerprint = 0, hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                async for packet in chunks:
                    size += len(packet)
                    require(size <= record["bytes"], "UPLOAD_SIZE_MISMATCH", "Upload exceeds its accepted byte count")
                    fingerprint.update(packet)
                    for offset in range(0, len(packet), 1024 * 1024):
                        await file_io(output.write, packet[offset:offset + 1024 * 1024])
                require(size == record["bytes"] and fingerprint.hexdigest() == record["sha256"], "UPLOAD_CONTENT_MISMATCH", "Upload bytes do not match their accepted size and hash")
                await file_io(output.flush)
                await file_io(os.fsync, output.fileno())
            await file_io(os.replace, temporary, destination)
            return await self._commit_upload(record, destination, consumer)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def commit_upload(self, upload_id: str, project_id: str, *, consumer_id: str | None = None):
        """Verify a fully received file after a lost commit response; never resend it."""
        self.host.assert_open()
        consumer = self.consumer(consumer_id)
        record = await self.get_upload(upload_id, project_id, consumer_id=consumer)
        if record["state"] == "completed":
            return record
        path = safe_child(self.host.root / "artifacts", upload_id)
        def verify():
            require(path.is_file() and path.stat().st_size == record["bytes"], "UPLOAD_INCOMPLETE", "The complete accepted file has not been received")
            with path.open("rb") as source:
                fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
            require(fingerprint == record["sha256"], "UPLOAD_CONTENT_MISMATCH", "Received bytes differ from the accepted hash")
        await file_io(verify)
        return await self._commit_upload(record, path, consumer)

    async def aclose(self):
        tasks = tuple(self.receiving)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def for_run(self, run_id: str):
        return await self.host.store.call(lambda db: self.list_for_run(db, run_id))

    def list_for_run(self, db, run_id):
        records = rows(db, "SELECT * FROM artifacts WHERE run_id=? ORDER BY rowid", (run_id,))
        return [self.view(row) for row in records]

    async def list(self, session_id: str | None, project_id: str, *, cursor: int = 0, limit: int = 25, query: str = ""):
        """Read a project-scoped session artifact page with source attribution."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100 and len(query) <= 200, "INVALID_REQUEST", "Read a bounded artifact page and search")
        def read(db):
            require(one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,)), "NOT_FOUND", "Project does not exist")
            if session_id is not None:
                session = one(db, "SELECT project_id FROM sessions WHERE id=?", (session_id,))
                require(session and session["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            return rows(db, "SELECT a.rowid AS position,a.*,i.agent_id,i.name AS instance_name,json_extract(d.config_json,'$.name') AS agent_name FROM artifacts a LEFT JOIN instances i ON i.id=a.instance_id LEFT JOIN agent_definitions d ON d.id=i.agent_id WHERE (? IS NULL OR a.session_id=?) AND a.project_id=? AND (?=0 OR a.rowid<?) AND (?='' OR instr(lower(a.name||' '||COALESCE(json_extract(d.config_json,'$.name'),'')||' '||COALESCE(i.agent_id,'')),lower(?))>0) ORDER BY a.rowid DESC LIMIT ?", (session_id, session_id, project_id, cursor, cursor, query, query, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [self.view(value) | {key: value[key] for key in ("position", "created_at", "agent_id", "agent_name", "instance_name")} for value in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    @staticmethod
    def view(row):
        return {key: row[key] for key in ("id", "name", "sha256", "bytes", "media_type", "project_id", "session_id", "origin", "run_id", "instance_id")}

    async def get(self, artifact_id: str):
        record = await self.host.store.call(lambda db: one(db, "SELECT * FROM artifacts WHERE id=?", (artifact_id,)))
        require(record is not None, "NOT_FOUND", "Artifact does not exist")
        return self.view(record) | {"content_view": await self.capture_view(record)}

    async def capture_view(self, record):
        # A managed log name narrows the lookup; the saved artifact ID and run
        # must also match the original command receipt before using its facts.
        capture = None
        if record["run_id"] and record["name"].endswith((".stdout.txt", ".stderr.txt")):
            action_id = record["name"][:-11]
            capture = await self.host.store.call(lambda db: one(db,
                "SELECT json_extract(v.value,'$.truncated') AS truncated,json_extract(v.value,'$.observed_bytes') AS observed_bytes FROM actions a,json_each(a.result_json,'$.output_refs') v WHERE a.id=? AND a.run_id=? AND a.name='run_command' AND json_extract(v.value,'$.artifact.id')=?",
                (action_id, record["run_id"], record["id"])))
        complete = not bool(capture["truncated"]) if capture is not None else True if record["origin"] == "upload" else None
        view = content_view("full", integrity="complete" if complete is True else "truncated" if complete is False else "unknown",
                            source_ref=record["id"], source_complete=complete, length=record["bytes"], offset_unit="byte")
        if capture is not None:
            view.update(observed_bytes=capture["observed_bytes"], saved_bytes=record["bytes"])
        return view

    async def read(self, artifact_id: str, project_id: str, *, offset: int = 0, limit: int = 8192):
        """Read a bounded UTF-8 artifact page; offsets refer to bytes."""
        record = await self.host.store.call(lambda db: one(db, "SELECT * FROM artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)))
        require(record is not None, "PERMISSION_DENIED", "Artifact belongs to another project or is unavailable")
        return await self._page(record, offset, limit)

    async def _page(self, record, offset, limit):
        require(0 <= offset < 2**63 and 64 <= limit <= 16384, "INVALID_REQUEST", "Use the returned byte offset and a bounded artifact page")
        require(record["media_type"].startswith("text/") or record["media_type"] in {"application/json", "application/xml"},
            "ARTIFACT_BINARY", "Use the artifact download for this binary format")
        path = safe_child(self.host.root / "artifacts", record["id"])
        require(path == Path(record["path"]).resolve(), "ARTIFACT_INVALID", "Artifact escaped its managed directory")
        captured = await self.capture_view(record)
        def page():
            with path.open("rb") as source:
                require(path.stat().st_size == record["bytes"], "ARTIFACT_CHANGED", "Published artifact size changed")
                source.seek(offset)
                raw = source.read(limit)
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                text = decoder.decode(raw, final=offset + len(raw) >= record["bytes"])
            except UnicodeDecodeError:
                raise RuntimeFault("ARTIFACT_ENCODING", "Read UTF-8 text from its returned byte offset") from None
            consumed = len(raw) - len(decoder.getstate()[0])
            next_offset = offset + consumed if offset + consumed < record["bytes"] else None
            return {"artifact": self.view(record) | {"content_view": captured}, "text": text, "offset": offset, "offset_unit": "byte",
                "next_offset": next_offset, "content_view": {**captured, "view": "page", "range": {"offset": offset, "length": consumed, "unit": "byte"}, "next_offset": next_offset}}
        return await asyncio.to_thread(page)

    def authorize(self, db, run_id, instance, artifact_id):
        record = one(db, "SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        require(record is not None and record["project_id"] == instance["project_id"], "PERMISSION_DENIED", "Artifact is outside this project")
        if record["instance_id"] == instance["id"] or one(db, "SELECT 1 FROM run_artifacts WHERE run_id=? AND artifact_id=?", (run_id, artifact_id)):
            return record
        require(record["origin"] == "run", "PERMISSION_DENIED", "Assign this source explicitly before the Run can read it")
        actor = self.host.coordination.actor(db, run_id)
        self.host.coordination.related(db, actor, record["run_id"])
        return record

    def assign(self, db, run_id, instance, artifact_ids, parent=None):
        parent_instance = one(db, "SELECT * FROM instances WHERE id=?", (parent["instance_id"],)) if parent and artifact_ids else None
        for identity in artifact_ids:
            record = one(db, "SELECT project_id,session_id FROM artifacts WHERE id=?", (identity,))
            require(record and record["project_id"] == instance["project_id"] and record["session_id"] in {None, instance["session_id"]},
                    "PERMISSION_DENIED", "Assigned artifact belongs to another project or session")
            if parent:
                self.authorize(db, parent["id"], parent_instance, identity)
            db.execute("INSERT INTO run_artifacts VALUES(?,?)", (run_id, identity))

    async def authorize_for_run(self, context, artifact_id):
        return await self.host.store.call(lambda db: self.authorize(db, context.run["id"], context.instance, artifact_id))

    async def image_for_run(self, run_id, artifact_id):
        context = self.host.active.get(run_id)
        require(context is not None, "STALE_RUN", "Run no longer owns this image read")
        record = await self.authorize_for_run(context, artifact_id)
        path = safe_child(self.host.root / "artifacts", artifact_id)
        require(path == Path(record["path"]).resolve(), "ARTIFACT_INVALID", "Assigned image escaped its managed directory")
        return path, self.view(record)

    async def read_for_run(self, context, artifact_id, *, offset=0, limit=8192):
        record = await self.authorize_for_run(context, artifact_id)
        return await self._page(record, offset, limit)
