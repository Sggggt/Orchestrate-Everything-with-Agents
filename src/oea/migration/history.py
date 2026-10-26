"""Bounded, project-scoped views of imported facts, isolated from model context."""

import json

from oea.contracts.errors import require
from oea.storage.persistence import one, rows


def _page(cursor, limit):
    require(type(cursor) is int and 0 <= cursor < 2**63 and type(limit) is int and 1 <= limit <= 100,
            "INVALID_REQUEST", "Read a bounded history page")


class ImportedHistory:
    def __init__(self, host):
        self.host = host

    async def summary(self, project_id: str) -> dict:
        """Count archived facts and records needing review in one project."""
        def read(db):
            require(one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,)), "NOT_FOUND", "Project does not exist")
            counts = rows(db, "SELECT table_name,COUNT(*) AS count,SUM(review_state='needs_review') AS needs_review FROM imported_history WHERE project_id=? GROUP BY table_name ORDER BY table_name", (project_id,))
            return {"project_id": project_id, "tables": counts, "history_only": True, "executable": False}
        return await self.host.store.call(read)

    async def list(self, project_id: str, table_name: str, *, session_id: str | None = None,
                   instance_id: str | None = None, needs_review: bool = False, cursor: int = 0, limit: int = 25) -> dict:
        """Page archived row metadata without loading its full content."""
        _page(cursor, limit)
        def read(db):
            query = "SELECT position,table_name,row_key,sha256,session_id,instance_id,review_state,created_at FROM imported_history WHERE project_id=? AND table_name=? AND position>?"
            params = [project_id, table_name, cursor]
            for field, value in (("session_id", session_id), ("instance_id", instance_id)):
                if value is not None:
                    query += " AND " + field + "=?"
                    params.append(value)
            if needs_review:
                query += " AND review_state='needs_review'"
            return rows(db, query + " ORDER BY position LIMIT ?", (*params, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None, "history_only": True, "executable": False}

    async def get(self, project_id: str, position: int) -> dict:
        """Read a complete original row owned by the selected project."""
        _page(position, 1)
        require(position > 0, "INVALID_REQUEST", "Choose an imported record position")
        def read(db):
            record = one(db, "SELECT * FROM imported_history WHERE project_id=? AND position=?", (project_id, position))
            require(record is not None, "NOT_FOUND", "Imported record does not exist in this project")
            record["record"] = json.loads(record.pop("row_json"))
            return {**record, "history_only": True, "executable": False}
        return await self.host.store.call(read)

    async def conversation(self, project_id: str, *, session_id: str | None = None, instance_id: str | None = None,
                           before: int | None = None, limit: int = 25) -> dict:
        """Read an imported transcript backwards, including original read receipts."""
        require(session_id is not None or instance_id is not None, "INVALID_REQUEST", "Choose a session or instance")
        _page(0 if before is None else before, limit)
        def read(db):
            query = "SELECT h.position,h.sha256,t.session_id,t.instance_id,t.source_position,t.role,t.created_at,t.read_at,json_extract(h.row_json,'$.id') AS id,json_extract(h.row_json,'$.content') AS content,json_extract(h.row_json,'$.summary_markdown') AS summary_markdown FROM imported_transcript t JOIN imported_history h ON h.position=t.history_position WHERE h.project_id=?"
            params = [project_id]
            for field, value in (("session_id", session_id), ("instance_id", instance_id)):
                if value is not None:
                    query += " AND t." + field + "=?"
                    params.append(value)
            if before is not None:
                query += " AND h.position<?"
                params.append(before)
            return rows(db, query + " ORDER BY h.position DESC LIMIT ?", (*params, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None, "history_only": True, "executable": False}

    async def usage(self, project_id: str, *, session_id: str | None = None) -> dict:
        """Read historical usage by kind and state alongside original budget totals."""
        def read(db):
            query = "SELECT json_extract(row_json,'$.kind') AS kind,json_extract(row_json,'$.state') AS state,COUNT(*) AS entries,COALESCE(SUM(json_extract(row_json,'$.input_tokens')),0) AS input_tokens,COALESCE(SUM(json_extract(row_json,'$.output_tokens')),0) AS output_tokens FROM imported_history WHERE project_id=? AND table_name='budget_entries'"
            params = [project_id]
            if session_id is not None:
                query += " AND session_id=?"
                params.append(session_id)
            grouped = rows(db, query + " GROUP BY kind,state ORDER BY kind,state", params)
            cumulative_query = "SELECT COUNT(*) AS budgets,COALESCE(SUM(json_extract(row_json,'$.model_calls')),0) AS model_calls,COALESCE(SUM(json_extract(row_json,'$.tool_calls')),0) AS tool_calls,COALESCE(SUM(json_extract(row_json,'$.input_tokens')),0) AS input_tokens,COALESCE(SUM(json_extract(row_json,'$.output_tokens')),0) AS output_tokens FROM imported_history WHERE project_id=? AND table_name='execution_budgets'"
            if session_id is not None:
                cumulative_query += " AND session_id=?"
            cumulative = one(db, cumulative_query, params)
            return {"project_id": project_id, "session_id": session_id, "items": grouped,
                "entries": sum(row["entries"] for row in grouped), "input_tokens": sum(row["input_tokens"] for row in grouped),
                "output_tokens": sum(row["output_tokens"] for row in grouped),
                "model_calls": sum(row["entries"] for row in grouped if row["kind"] == "model"),
                "tool_calls": sum(row["entries"] for row in grouped if row["kind"] == "tool"),
                "cumulative": cumulative if cumulative["budgets"] else None, "history_only": True, "executable": False}
        return await self.host.store.call(read)
