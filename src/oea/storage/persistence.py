"""Single-owner SQLite facts; graph checkpoints have their own saver."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import apsw

from oea.contracts.configuration import PROTOCOL_VERSION
from oea.contracts.errors import RuntimeFault
from oea.interfaces.registry import operation_context


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def now_ms():
    return time.time_ns() // 1_000_000


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
                      default=lambda item: item.model_dump(mode="json"))


def one(db, sql, params=()):
    return next(iter(db.execute(sql, params)), None)


def rows(db, sql, params=()):
    return list(db.execute(sql, params))


def event(db, kind, *, project_id=None, session_id=None, instance_id=None, run_id=None, intent_id=None, data=None):
    payload = dict(data or {})
    if intent_id is not None:
        payload["call_id"] = intent_id
    operation = operation_context.get()
    if operation:
        payload["operation"] = operation
    if project_id is None and run_id:
        db.execute("INSERT INTO events(kind,project_id,session_id,instance_id,run_id,data_json,created_at) SELECT ?,i.project_id,i.session_id,i.id,r.id,?,? FROM runs r JOIN instances i ON i.id=r.instance_id WHERE r.id=?",
            (kind, encode(payload), now_ms(), run_id))
    elif project_id is None and intent_id:
        db.execute("INSERT INTO events(kind,project_id,data_json,created_at) SELECT ?,project_id,?,? FROM tool_intents WHERE id=?", (kind, encode(payload), now_ms(), intent_id))
    elif project_id is None and session_id:
        db.execute("INSERT INTO events(kind,project_id,session_id,instance_id,run_id,data_json,created_at) SELECT ?,project_id,id,?,?,?,? FROM sessions WHERE id=?",
            (kind, instance_id, run_id, encode(payload), now_ms(), session_id))
    else:
        db.execute("INSERT INTO events(kind,project_id,session_id,instance_id,run_id,data_json,created_at) VALUES(?,?,?,?,?,?,?)",
                   (kind, project_id, session_id, instance_id, run_id, encode(payload), now_ms()))
    return db.last_insert_rowid()


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.executor = None
        self.db = None

    async def open(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oea-store")
        try:
            await self.call(self._open)
        except BaseException:
            await self.close()
            raise

    def _open(self, _):
        path = self.root / "oea.sqlite"
        if not path.exists() and (self.root / "runtime.sqlite").exists():
            raise RuntimeFault("MIGRATION_REQUIRED", "Use offline migration into a new state directory before opening legacy state")
        self.db = apsw.Connection(str(path))
        self.db.set_row_trace(lambda cursor, values: dict(zip((column[0] for column in cursor.get_description()), values)))
        self.db.set_busy_timeout(5000)
        self.db.pragma("journal_mode", "wal")
        self.db.pragma("synchronous", "full")
        self.db.pragma("foreign_keys", "on")
        script = Path(__file__).with_name("schema-v2.sql").read_text(encoding="utf-8")
        checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
        tables = rows(self.db, "SELECT name FROM sqlite_master WHERE type='table'")
        if tables:
            if not any(table["name"] == "metadata" for table in tables):
                raise RuntimeFault("SCHEMA_MISMATCH", "State format is not recognized")
            version = one(self.db, "SELECT value FROM metadata WHERE key='protocol_version'")
            if version is None or version["value"] != PROTOCOL_VERSION:
                raise RuntimeFault("SCHEMA_MISMATCH", "State version differs from this application; use offline migration")
            installed = one(self.db, "SELECT value FROM metadata WHERE key='schema_sha256'")
            if installed is None or installed["value"] != checksum:
                raise RuntimeFault("SCHEMA_MISMATCH", "State schema differs from this application; stop the host and use 'oea upgrade-state' or the legacy converter")
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(script)
            self.db.execute("INSERT INTO metadata VALUES('protocol_version',?)", (PROTOCOL_VERSION,))
            self.db.execute("INSERT INTO metadata VALUES('schema_sha256',?)", (checksum,))
            self.db.execute("INSERT INTO metadata VALUES('schema_revision','2')")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    async def call(self, function, *, transaction=False):
        if self.executor is None:
            raise RuntimeFault("STORE_CLOSED", "State store is not open")
        def invoke():
            if transaction:
                self.db.execute("BEGIN IMMEDIATE")
            try:
                value = function(self.db)
                if transaction:
                    self.db.execute("COMMIT")
                return value
            except BaseException:
                if transaction:
                    self.db.execute("ROLLBACK")
                raise
        context = copy_context()
        future = asyncio.get_running_loop().run_in_executor(self.executor, context.run, invoke)
        # A caller leaving cannot turn a committed fact into an assumed failure.
        return await asyncio.shield(future)

    async def close(self):
        if self.executor is None:
            return
        if self.db is not None:
            await self.call(lambda db: db.close())
            self.db = None
        executor, self.executor = self.executor, None
        executor.shutdown(wait=True)
