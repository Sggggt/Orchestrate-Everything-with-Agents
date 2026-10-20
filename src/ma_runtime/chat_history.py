"""Append-only native chat checkpoints, with a bounded immutable-prefix cache."""

import json
from collections import OrderedDict

from .errors import require
from .models import json_dump, new_id, now_ms
from .store import one, rows

CHAT_VERSION = "chat-messages-v2"
LEGACY_CHAT_VERSION = "chat-messages-v1"


def conversation_rows(db, thread_id, *, after=0, before=9223372036854775807, limit=100,
                      descending=False, user_limit=9223372036854775807):
    direction = "DESC" if descending else "ASC"
    return rows(db, "WITH RECURSIVE lineage(thread_id,message_seq,user_seq) AS ("
        "SELECT ?,9223372036854775807,9223372036854775807 UNION ALL "
        "SELECT b.parent_thread_id,MIN(l.message_seq,b.message_seq),MIN(l.user_seq,b.user_seq) "
        "FROM thread_branches b JOIN lineage l ON b.thread_id=l.thread_id) "
        "SELECT c.*,r.state AS observed_state,r.error_json AS observed_error_json FROM conversation c JOIN lineage l ON c.thread_id=l.thread_id LEFT JOIN runs r ON r.id=c.run_id "
        "WHERE c.seq>? AND c.seq<? AND c.seq<=l.message_seq AND (c.role!='user' OR c.seq<=MIN(l.user_seq,?)) "
        "ORDER BY c.seq " + direction + " LIMIT ?", (thread_id, after, before, user_limit, limit))


def checkpoint_window(db, checkpoint, thread):
    saved = one(db, "SELECT user_seq,message_seq FROM checkpoint_windows WHERE checkpoint_id=?", (checkpoint.id,))
    if saved:
        return saved
    run = one(db, "SELECT task_id FROM runs WHERE id=?", (checkpoint.created_by_run_id,)) if checkpoint.created_by_run_id else None
    if run:
        cutoff = one(db, "SELECT COALESCE(MAX(c.seq),0) AS seq FROM conversation c LEFT JOIN tasks t ON t.id=c.task_id "
            "WHERE c.thread_id=? AND c.role='user' AND (c.task_id IS NULL OR t.rowid<=(SELECT rowid FROM tasks WHERE id=?))",
            (thread.id, run.task_id)).seq
    else:
        cutoff = one(db, "SELECT COALESCE(MAX(seq),0) AS seq FROM conversation WHERE thread_id=? AND role='user' AND created_at<=?",
                     (thread.id, checkpoint.created_at)).seq
    reply = one(db, "SELECT COALESCE(MAX(c.seq),0) AS seq FROM checkpoints p JOIN conversation c ON c.run_id=p.created_by_run_id "
                "WHERE p.thread_id=? AND p.revision<=?", (thread.id, checkpoint.revision)).seq
    imported = one(db, "SELECT COALESCE(MAX(seq),0) AS seq FROM conversation WHERE thread_id=? AND task_id IS NULL AND run_id IS NULL AND created_at<=?",
                   (thread.id, checkpoint.created_at)).seq
    inherited = one(db, "SELECT message_seq,user_seq FROM thread_branches WHERE thread_id=?", (thread.id,))
    cutoff = max(cutoff, inherited.user_seq if inherited else 0)
    ceiling = max(reply, imported, cutoff, inherited.message_seq if inherited else 0)
    db.execute("INSERT INTO checkpoint_windows VALUES(?,?,?)", (checkpoint.id, cutoff, ceiling))
    return one(db, "SELECT user_seq,message_seq FROM checkpoint_windows WHERE checkpoint_id=?", (checkpoint.id,))


def user_cutoff(db, thread, task):
    direct = one(db, "SELECT MAX(seq) AS seq FROM conversation WHERE task_id=? AND role='user'", (task.id,)).seq
    if direct is not None:
        return direct
    own = one(db, "SELECT COALESCE(MAX(c.seq),0) AS seq FROM conversation c LEFT JOIN tasks t ON t.id=c.task_id "
              "WHERE c.thread_id=? AND c.role='user' AND (c.task_id IS NULL OR t.rowid<=(SELECT rowid FROM tasks WHERE id=?))",
              (thread.id, task.id)).seq
    inherited = one(db, "SELECT user_seq FROM checkpoint_windows WHERE checkpoint_id=?", (thread.latest_checkpoint_id,)) if thread.latest_checkpoint_id else None
    return max(own, inherited.user_seq if inherited else 0)


class ChatHistory:
    def __init__(self):
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def remember(self, thread_id, head, messages, size):
        old = self.cache.pop(thread_id, None)
        if old:
            self.cache_bytes -= old[2]
        if size > 8 * 1024 * 1024:
            return
        self.cache[thread_id] = (head, tuple(messages), size)
        self.cache_bytes += size
        while len(self.cache) > 16 or self.cache_bytes > 8 * 1024 * 1024:
            _, entry = self.cache.popitem(last=False)
            self.cache_bytes -= entry[2]

    def head(self, db, checkpoint, thread):
        saved = one(db, "SELECT head_id FROM chat_checkpoint_heads WHERE checkpoint_id=?", (checkpoint.id,))
        if saved:
            return saved.head_id
        reference = json.loads(checkpoint.ref_json)
        if checkpoint.format_version == CHAT_VERSION:
            head = reference.get("message_head")
        else:
            require(checkpoint.format_version == LEGACY_CHAT_VERSION, "UNSUPPORTED_CAPABILITY", "Unsupported native chat checkpoint")
            messages = [message for message in reference.get("messages", []) if message.get("role") not in {"system", "developer"}]
            head = new_id("seg") if messages else None
            if head:
                encoded = json_dump(messages)
                db.execute("INSERT INTO chat_segments VALUES(?,NULL,?,?,?,?,?,?)",
                           (head, thread.session_id, thread.id, encoded, len(messages), len(encoded.encode("utf-8")), now_ms()))
                self.remember(thread.id, head, messages, len(encoded.encode("utf-8")))
        db.execute("INSERT INTO chat_checkpoint_heads VALUES(?,?)", (checkpoint.id, head))
        return head

    def load(self, db, checkpoint, thread):
        reference = json.loads(checkpoint.ref_json)
        head = self.head(db, checkpoint, thread)
        if head is None:
            return {"task_id": reference.get("task_id"), "message_head": None, "message_count": 0, "messages": []}
        cached = self.cache.get(thread.id)
        if cached and cached[0] == head:
            self.cache.move_to_end(thread.id)
            messages = cached[1]
        else:
            segments = rows(db, "WITH RECURSIVE chain AS (SELECT *,0 AS depth FROM chat_segments WHERE id=? UNION ALL "
                "SELECT s.*,c.depth+1 FROM chat_segments s JOIN chain c ON s.id=c.parent_id) SELECT * FROM chain ORDER BY depth", (head,))
            require(segments and all(segment.session_id == thread.session_id for segment in segments),
                    "INVALID_CHECKPOINT", "Native chat prefix is unavailable in this session")
            messages = tuple(message for segment in reversed(segments) for message in json.loads(segment.messages_json))
            self.remember(thread.id, head, messages, segments[0].total_bytes)
        return {"task_id": reference.get("task_id"), "message_head": head, "message_count": len(messages), "messages": messages}

    def append(self, db, reference, thread):
        previous = one(db, "SELECT * FROM checkpoints WHERE id=?", (thread.latest_checkpoint_id,)) if thread.latest_checkpoint_id else None
        expected = self.head(db, previous, thread) if previous else None
        require(reference.get("base_head") == expected, "REVISION_CONFLICT", "Native chat tail does not extend the current checkpoint")
        delta = reference.get("messages", [])
        require(isinstance(delta, list) and all(isinstance(message, dict) and message.get("role") in {"user", "assistant", "tool"} for message in delta),
                "ADAPTER_PROTOCOL_ERROR", "Native chat tails contain only conversation messages")
        parent = one(db, "SELECT message_count,total_bytes FROM chat_segments WHERE id=?", (expected,)) if expected else None
        count = (parent.message_count if parent else 0) + len(delta)
        head = expected
        if delta:
            head = new_id("seg")
            encoded = json_dump(delta)
            size = (parent.total_bytes if parent else 0) + len(encoded.encode("utf-8"))
            db.execute("INSERT INTO chat_segments VALUES(?,?,?,?,?,?,?,?)",
                       (head, expected, thread.session_id, thread.id, encoded, count, size, now_ms()))
            cached = self.cache.get(thread.id)
            if not expected or (cached and cached[0] == expected):
                self.remember(thread.id, head, (*(cached[1] if cached else ()), *delta), size)
        return {"task_id": reference.get("task_id"), "message_head": head, "message_count": count}
