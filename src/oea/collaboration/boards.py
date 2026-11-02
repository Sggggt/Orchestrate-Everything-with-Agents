"""Explicit shared boards and private inbox readers in the existing state store."""

from typing import Literal

from oea.catalog.service import need
from oea.contracts.errors import require
from oea.storage.persistence import event, new_id, now_ms, one, rows


class Boards:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def can_read(db, board, instance):
        if board["project_id"] != instance["project_id"] or board["session_id"] not in {None, instance["session_id"]}:
            return False
        return board["scope"] != "inbox" or bool(one(db, "SELECT 1 FROM board_readers WHERE board_id=? AND instance_id=?", (board["id"], instance["id"])))

    @staticmethod
    def check(db, board_id, *, project_id, instance=None, write=False, departments=None):
        board = need(db, "boards", board_id)
        require(board["project_id"] == project_id, "PERMISSION_DENIED", "Board belongs to another project")
        if instance:
            require(board["session_id"] in {None, instance["session_id"]}, "PERMISSION_DENIED", "Board belongs to another session")
            if write and board["scope"] == "inbox":
                owner = need(db, "instances", board["owner_instance_id"])
                require(owner["agent_id"] in departments, "PERMISSION_DENIED", "Inbox owner is not currently reachable")
            else:
                require(Boards.can_read(db, board, instance), "PERMISSION_DENIED", "This private inbox has no read grant for the instance")
        return board

    def _create(self, db, project_id, name, scope, session_id=None, owner_instance_id=None):
        need(db, "projects", project_id)
        require(scope in {"project", "session", "inbox"}, "INVALID_REQUEST", "Choose a board scope")
        require(0 < len(name.strip()) <= 120, "INVALID_REQUEST", "Supply a bounded board name")
        if scope == "project":
            require(session_id is None and owner_instance_id is None, "INVALID_REQUEST", "Project boards have no session or private owner")
        else:
            require(session_id is not None and need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
        if scope == "inbox":
            owner = need(db, "instances", owner_instance_id)
            require(owner["session_id"] == session_id, "PERMISSION_DENIED", "Inbox owner belongs to another session")
        else:
            require(owner_instance_id is None, "INVALID_REQUEST", "Only inboxes have private owners")
        previous = one(db, "SELECT * FROM boards WHERE project_id=? AND scope=? AND session_id IS ? AND owner_instance_id IS ? AND name=?", (project_id, scope, session_id, owner_instance_id, name.strip()))
        if previous:
            return previous
        identity = new_id("board")
        db.execute("INSERT INTO boards VALUES(?,?,?,?,?,?,?)", (identity, project_id, session_id, scope, owner_instance_id, name.strip(), now_ms()))
        if owner_instance_id:
            db.execute("INSERT INTO board_readers VALUES(?,?)", (identity, owner_instance_id))
        event(db, "board.created", project_id=project_id, session_id=session_id, data={"board_id": identity})
        return need(db, "boards", identity)

    async def create(self, project_id: str, name: str, *, scope: Literal["project", "session", "inbox"] = "session", session_id: str | None = None, owner_instance_id: str | None = None):
        value = await self.host.store.call(lambda db: self._create(db, project_id, name, scope, session_id, owner_instance_id), transaction=True)
        self.host.notify()
        return value

    async def set_reader(self, board_id: str, instance_id: str, allowed: bool, *, project_id: str):
        def change(db):
            board = self.check(db, board_id, project_id=project_id)
            instance = need(db, "instances", instance_id)
            require(board["scope"] == "inbox" and instance["session_id"] == board["session_id"], "PERMISSION_DENIED", "Private readers must belong to the inbox session")
            require(allowed or instance_id != board["owner_instance_id"], "INVALID_REQUEST", "The inbox owner retains read access")
            if allowed:
                db.execute("INSERT OR IGNORE INTO board_readers VALUES(?,?)", (board_id, instance_id))
            else:
                db.execute("DELETE FROM board_readers WHERE board_id=? AND instance_id=?", (board_id, instance_id))
            event(db, "board.reader_changed", project_id=project_id, session_id=board["session_id"], data={"board_id": board_id, "instance_id": instance_id, "allowed": allowed})
            return {"board_id": board_id, "instance_id": instance_id, "allowed": allowed}
        result = await self.host.store.call(change, transaction=True)
        self.host.notify()
        return result

    @staticmethod
    def page(db, project_id, session_id, instance=None, cursor=0, limit=25):
        require(type(cursor) is int and cursor >= 0 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded board page")
        query = "SELECT b.rowid AS position,b.* FROM boards b WHERE b.project_id=? AND b.rowid>?"
        params = [project_id, cursor]
        if session_id:
            require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            query += " AND (b.session_id=? OR b.session_id IS NULL)"
            params.append(session_id)
        if instance:
            query += " AND (b.scope!='inbox' OR EXISTS(SELECT 1 FROM board_readers r WHERE r.board_id=b.id AND r.instance_id=?))"
            params.append(instance["id"])
        values = rows(db, query + " ORDER BY b.rowid LIMIT ?", (*params, limit + 1))
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def list(self, project_id: str, *, session_id: str | None = None, cursor: int = 0, limit: int = 25):
        return await self.host.store.call(lambda db: self.page(db, project_id, session_id, cursor=cursor, limit=limit))

    async def get(self, board_id: str, *, project_id: str):
        return await self.host.store.call(lambda db: self.check(db, board_id, project_id=project_id))
