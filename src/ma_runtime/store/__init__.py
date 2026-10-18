"""A single connection, owned by a dedicated executor thread."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar

import apsw

from ..errors import RuntimeFault, require
from ..models import View, json_dump, new_id, now_ms

T = TypeVar("T")


def one(db, sql, params=()):
    return next(iter(db.execute(sql, params)), None)


def rows(db, sql, params=()):
    return list(db.execute(sql, params))


def emit(
    db,
    event_type: str,
    *,
    session_id=None,
    thread_id=None,
    task_id=None,
    run_id=None,
    payload=None,
):
    event_id = new_id("evt")
    db.execute(
        "INSERT INTO events(id,session_id,thread_id,task_id,run_id,type,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            event_id,
            session_id,
            thread_id,
            task_id,
            run_id,
            event_type,
            json_dump(payload or {}),
            now_ms(),
        ),
    )
    return db.last_insert_rowid()


class StoreActor:
    def __init__(self, root: Path):
        self.root = root
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ma-store")
        self.db = None
        self.failed = False
        self.closed = False

    async def open(self):
        await self.call(self._open)

    def _open(self, _):
        version = tuple(map(int, apsw.sqlitelibversion().split(".")))
        require(
            version >= (3, 51, 3),
            "STORE_UNAVAILABLE",
            "SQLite 3.51.3 or newer is required",
        )
        self.db = apsw.Connection(str(self.root / "runtime.sqlite"))
        self.db.set_row_trace(
            lambda cursor, row: View(zip((d[0] for d in cursor.get_description()), row))
        )
        self.db.set_busy_timeout(5000)
        for name, value in (
            ("journal_mode", "WAL"),
            ("synchronous", "FULL"),
            ("foreign_keys", "ON"),
        ):
            self.db.pragma(name, value)
        require(
            str(self.db.pragma("journal_mode")).lower() == "wal"
            and self.db.pragma("synchronous") == 2
            and self.db.pragma("foreign_keys") == 1,
            "STORE_UNAVAILABLE",
            "Required SQLite durability settings are unavailable",
        )
        sources = [
            Path(__file__).with_name("schema.sql"),
            Path(__file__).with_name("schema-2.sql"),
            Path(__file__).with_name("schema-3.sql"),
            Path(__file__).with_name("schema-4.sql"),
            Path(__file__).with_name("schema-5.sql"),
            Path(__file__).with_name("schema-6.sql"),
            Path(__file__).with_name("schema-7.sql"),
            Path(__file__).with_name("schema-8.sql"),
            Path(__file__).with_name("schema-9.sql"),
            Path(__file__).with_name("schema-10.sql"),
        ]
        scripts = [source.read_text(encoding="utf-8") for source in sources]
        checksums = [hashlib.sha256(script.encode()).hexdigest() for script in scripts]
        exists = one(
            self.db, "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
        )
        migrations = (
            rows(self.db, "SELECT * FROM schema_migrations ORDER BY version")
            if exists
            else []
        )
        if not exists and one(
            self.db, "SELECT name FROM sqlite_master WHERE type='table'"
        ):
            raise RuntimeFault("SCHEMA_MISMATCH", "Unrecognized database schema")
        require(
            len(migrations) <= len(scripts)
            and all(
                migration.version == index + 1
                and migration.checksum == checksums[index]
                for index, migration in enumerate(migrations)
            ),
            "SCHEMA_MISMATCH",
            "Database schema is newer or differs from the installed migrations",
        )
        for index in range(len(migrations), len(scripts)):
            version = index + 1
            backup_dir = self.root / "backups"
            backup_dir.mkdir(exist_ok=True)
            before = apsw.Connection(
                str(backup_dir / f"before-schema-{version}.sqlite")
            )
            try:
                with before.backup("main", self.db, "main") as backup:
                    backup.step(-1)
            finally:
                before.close()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(scripts[index])
                self.db.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?)",
                    (version, checksums[index], now_ms()),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        require(
            next(iter(self.db.execute("PRAGMA quick_check"))).get("quick_check")
            == "ok",
            "STORE_UNAVAILABLE",
            "Database integrity check failed",
        )

    async def call(self, fn: Callable[..., T], *, transaction=False) -> T:
        require(not self.closed, "STORE_UNAVAILABLE", "State store is closed")
        loop = asyncio.get_running_loop()

        def invoke():
            if transaction:
                self.db.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self.db)
                if transaction:
                    self.db.execute("COMMIT")
                return result
            except BaseException as exc:
                critical = isinstance(
                    exc,
                    (
                        apsw.IOError,
                        apsw.FullError,
                        apsw.CorruptError,
                        apsw.CantOpenError,
                    ),
                )
                self.failed = self.failed or critical
                if transaction:
                    try:
                        self.db.execute("ROLLBACK")
                    except apsw.Error:
                        self.failed = True
                        critical = True
                if critical:
                    raise RuntimeFault(
                        "STORE_UNAVAILABLE", "The state store could not reliably commit"
                    ) from None
                raise

        return await asyncio.shield(loop.run_in_executor(self.executor, invoke))

    async def backup(self, destination: Path):
        def copy(db):
            target = apsw.Connection(str(destination))
            try:
                with target.backup("main", db, "main") as backup:
                    backup.step(-1)
            finally:
                target.close()

        await self.call(copy)

    async def close(self):
        try:
            if self.db is not None:
                await self.call(lambda db: db.close())
        finally:
            self.closed = True
            self.executor.shutdown(wait=True)
