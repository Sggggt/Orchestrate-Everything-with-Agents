"""Bounded browser projections and durable, per-message read state."""

import json

from .errors import execution_state, require
from .models import PageEnvelope, now_ms
from .presentation import markdown_summary, delivery_summary
from .store import emit, one, rows


ACTIVITY_JOINS = """
    LEFT JOIN conversation c ON i.kind='conversation' AND c.id=i.source_id
    LEFT JOIN messages m ON i.kind='message' AND m.id=i.source_id
    LEFT JOIN operations_reports r ON i.kind='report' AND r.id=i.source_id
"""
ACTIVITY_TEXT = """CASE WHEN json_extract(c.metadata_json,'$.acceptance.state')='FAILED'
    THEN substr(c.content,1,1600) ELSE COALESCE(NULLIF(c.summary_markdown,''),substr(c.content,1,1600),r.summary,
    substr(json_extract(m.envelope_json,'$.payload.summary_markdown'),1,1600),
    substr(json_extract(m.envelope_json,'$.payload.text'),1,1600),
    substr(json_extract(m.envelope_json,'$.payload.input'),1,1600),'协作消息已保存。') END"""


class Supervision:
    def __init__(self, runtime):
        self.rt = runtime

    @staticmethod
    def display_positions(db, messages, reports):
        wanted = {(message.source_thread_id or message.target_thread_id, "message", message.id) for message in messages}
        wanted.update((report["thread_id"], "report", report["id"]) for report in reports)
        if not wanted:
            return {}
        identities = tuple({identity for _, _, identity in wanted})
        marks = rows(db, "SELECT thread_id,kind,source_id,position,read_at FROM thread_activity_items WHERE source_id IN ("
                     + ",".join("?" for _ in identities) + ")", identities)
        return {mark.source_id: {"thread_id": mark.thread_id, "position": mark.position, "read": mark.read_at is not None}
                for mark in marks if (mark.thread_id, mark.kind, mark.source_id) in wanted}

    @staticmethod
    def summaries(db, session_id, *, agent_id=None, cursor=None, limit=None):
        params, where = [session_id], "h.session_id=?"
        if agent_id is not None:
            where += " AND h.agent_id=?"
            params.append(agent_id)
        if cursor is not None:
            try:
                position, created, identity = json.loads(cursor)
            except (ValueError, TypeError):
                require(False, "INVALID_REQUEST", "Invalid instance cursor")
            require(type(position) is int and type(created) is int and isinstance(identity, str),
                    "INVALID_REQUEST", "Invalid instance cursor")
            where += " AND (COALESCE(a.latest_position,0),h.created_at,h.id)<(?,?,?)"
            params.extend((position, created, identity))
        tail = " ORDER BY COALESCE(a.latest_position,0) DESC,h.created_at DESC,h.id DESC"
        if limit is not None:
            tail += " LIMIT ?"
            params.append(limit)
        found = rows(db, """SELECT h.id AS thread_id,h.agent_id,h.title,h.created_at,
            COALESCE(a.latest_position,0) AS activity_position,COALESCE(a.unread_count,0) AS unread_count,
            i.created_at AS activity_at,i.kind AS activity_kind,i.source_id AS activity_id,
            t.id AS task_id,t.state,t.error_json,
            substr(json_extract(t.spec_json,'$.payload.input'),1,160) AS goal,
            json_extract(t.result_json,'$.metadata.acceptance.state') AS delivery_state,
            json_extract(c.metadata_json,'$.acceptance.state') AS activity_delivery_state,
            w.id AS workflow_run_id,w.workflow_id,w.revision AS workflow_revision,f.name AS workflow_name,
            """ + ACTIVITY_TEXT + """ AS summary
            FROM threads h LEFT JOIN thread_activity_state a ON a.thread_id=h.id
            LEFT JOIN thread_activity_items i ON i.position=a.latest_position
            LEFT JOIN tasks t ON t.id=COALESCE(h.owner_task_id,
                (SELECT id FROM tasks WHERE thread_id=h.id ORDER BY created_at DESC,rowid DESC LIMIT 1))
            LEFT JOIN thread_workflow_runs o ON o.thread_id=h.id
            LEFT JOIN workflow_runs w ON w.id=o.run_id LEFT JOIN workflows f ON f.id=w.workflow_id
            """ + ACTIVITY_JOINS + " WHERE " + where + tail, params)
        result = []
        for row in found:
            item = dict(row)
            error = json.loads(item.pop("error_json")) if row.error_json else None
            item["execution_state"] = execution_state(row.state, error) if row.state else "READY"
            project = delivery_summary if item.pop("activity_delivery_state") == "FAILED" else markdown_summary
            item["summary_markdown"] = project(item.pop("summary"), limit=320) if row.activity_position else ""
            result.append(item)
        return result

    async def instances(self, session_id, agent_id=None, cursor=None, limit=25):
        from .services import need
        require(type(limit) is int and 1 <= limit <= 100, "INVALID_REQUEST", "Use a page size of 1..100")
        def read(db):
            need(db, "sessions", session_id)
            found = self.summaries(db, session_id, agent_id=agent_id, cursor=cursor, limit=limit + 1)
            items = found[:limit]
            last = items[-1] if items else None
            return PageEnvelope(items=items, has_more=len(found) > limit,
                cursor=json.dumps([last["activity_position"], last["created_at"], last["thread_id"]]) if last else cursor)
        return await self.rt.store.call(read)

    async def activity(self, thread_id, cursor=None, limit=50):
        from .services import need
        require(type(limit) is int and 1 <= limit <= 100, "INVALID_REQUEST", "Use a page size of 1..100")
        require(cursor is None or (isinstance(cursor, str) and cursor.isdecimal()), "INVALID_REQUEST", "Invalid activity cursor")
        def read(db):
            need(db, "threads", thread_id)
            found = rows(db, "SELECT i.*,COALESCE(c.task_id,r.task_id) AS task_id,COALESCE(d.state,m.state) AS delivery_stage,"
                "r.wait_for_guidance,r.reply_message_id,json_extract(c.metadata_json,'$.acceptance.state') AS activity_delivery_state," + ACTIVITY_TEXT + " AS summary FROM thread_activity_items i "
                + ACTIVITY_JOINS + " LEFT JOIN message_delivery d ON d.message_id=m.id WHERE i.thread_id=? AND i.position<? ORDER BY i.position DESC LIMIT ?",
                (thread_id, int(cursor) if cursor else 9223372036854775807, limit + 1))
            items = []
            for row in found[:limit]:
                item = dict(row)
                project = delivery_summary if item.pop("activity_delivery_state") == "FAILED" else markdown_summary
                item["summary_markdown"] = project(item.pop("summary"), limit=1200)
                item["read"] = row.read_at is not None
                items.append(item)
            return PageEnvelope(items=items, has_more=len(found) > limit,
                                cursor=str(items[-1]["position"]) if items else cursor)
        return await self.rt.store.call(read)

    async def mark_read(self, thread_id, positions):
        from .services import need
        self.rt.admit()
        require(isinstance(positions, (list, tuple)) and 0 < len(positions) <= 100
                and all(type(position) is int and position > 0 for position in positions),
                "INVALID_REQUEST", "Provide 1..100 rendered message positions")
        positions = tuple(set(positions))
        placeholders = ",".join("?" for _ in positions)
        def update(db):
            thread = need(db, "threads", thread_id)
            count = one(db, f"SELECT COUNT(*) AS count FROM thread_activity_items WHERE thread_id=? AND position IN ({placeholders})",
                        (thread_id, *positions)).count
            require(count == len(positions), "PERMISSION_DENIED", "Read positions must belong to this thread")
            stamp = now_ms()
            db.execute(f"UPDATE thread_activity_items SET read_at=? WHERE thread_id=? AND read_at IS NULL AND position IN ({placeholders})",
                       (stamp, thread_id, *positions))
            changed = db.changes()
            if changed:
                emit(db, "thread.read", session_id=thread.session_id, thread_id=thread_id,
                     payload={"positions": sorted(positions)})
            unread = one(db, "SELECT unread_count FROM thread_activity_state WHERE thread_id=?", (thread_id,))
            return {"thread_id": thread_id, "positions": sorted(positions), "changed": changed,
                    "unread_count": unread.unread_count if unread else 0}
        result = await self.rt.store.call(update, transaction=True)
        if result["changed"]:
            self.rt.notify()
        return result
