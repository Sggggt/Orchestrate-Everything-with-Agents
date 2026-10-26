"""Bounded durable observations, independent of transient model progress."""

import json

from oea.catalog.service import need
from oea.contracts.errors import require
from oea.storage.persistence import one, rows


class Events:
    def __init__(self, host):
        self.host = host

    async def list(self, project_id: str, *, session_id: str | None = None, run_id: str | None = None, after: int = 0, limit: int = 100):
        require(0 <= after < 2**63 and 1 <= limit <= 250, "INVALID_REQUEST", "Read a bounded durable event page")
        def read(db):
            project = one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,))
            if project is None:
                require(session_id is None and run_id is None and one(db, "SELECT 1 FROM events WHERE project_id=? AND kind='project.deleted' LIMIT 1", (project_id,)),
                        "NOT_FOUND", "Project does not exist")
            if session_id:
                require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            if run_id:
                run = need(db, "runs", run_id)
                instance = need(db, "instances", run["instance_id"])
                require(instance["project_id"] == project_id and session_id in {None, instance["session_id"]}, "PERMISSION_DENIED", "Run belongs to another event scope")
            query, params = "SELECT * FROM events WHERE project_id=? AND position>?", [project_id, after]
            for column, value in (("session_id", session_id), ("run_id", run_id)):
                if value:
                    query += " AND " + column + "=?"
                    params.append(value)
            return rows(db, query + " ORDER BY position LIMIT ?", (*params, limit + 1)), project is None
        values, deleted = await self.host.store.call(read)
        page = values[:limit]
        return {"items": [{key: row[key] for key in ("position", "kind", "project_id", "session_id", "instance_id", "run_id", "created_at")} | {"data": json.loads(row["data_json"])} for row in page],
            "position": page[-1]["position"] if page else after, "cursor": page[-1]["position"] if len(values) > limit else None,
            "project_deleted": deleted}

    async def stream(self, project_id, *, session_id=None, after=0):
        position = after
        while self.host.opened and not self.host.closing:
            changed = self.host.changed
            page = await self.list(project_id, session_id=session_id, after=position)
            for value in page["items"]:
                yield value
            position = page["position"]
            if page["project_deleted"]:
                return
            if page["cursor"] is None:
                await changed.wait()
