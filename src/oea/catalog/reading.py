"""Reader-owned conversation cursors and indexed unread projections."""

from oea.contracts.errors import require
from oea.interfaces.registry import operation_context
from oea.storage.persistence import one, rows

VISIBLE_REPLY = "length(trim(COALESCE(json_extract(c.body_json,'$.content'),json_extract(a.result_json,'$.message.content'),json_extract(a.result_json,'$.text'),'')))>0"


def reader(consumer_id=None, *, required=True):
    context = operation_context.get() or {}
    identity = consumer_id or context.get("consumer_id")
    if identity is None and not required:
        return None
    require(isinstance(identity, str) and 0 < len(identity) <= 128 and (not context.get("consumer_id") or identity == context["consumer_id"]),
            "PERMISSION_DENIED", "Use the current conversation reader identity")
    return identity


def summary(db, session_id, project_id, consumer_id):
    position = one(db, "SELECT position FROM read_positions WHERE consumer_id=? AND session_id=?", (consumer_id, session_id))
    position = position["position"] if position else 0
    count = one(db, "SELECT COUNT(*) AS count FROM messages WHERE session_id=? AND position>?", (session_id, position))["count"]
    agents = rows(db, "SELECT i.agent_id,COUNT(DISTINCT i.id) AS instances,SUM(CASE WHEN " + VISIBLE_REPLY + " THEN 1 ELSE 0 END) AS count "
        "FROM instances i LEFT JOIN instance_read_positions p ON p.instance_id=i.id AND p.consumer_id=? "
        "LEFT JOIN conversation c ON c.instance_id=i.id AND c.role='assistant' AND c.position>COALESCE(p.position,0) "
        "LEFT JOIN actions a ON a.id=c.action_id WHERE i.session_id=? GROUP BY i.agent_id", (consumer_id, session_id))
    revision = one(db, "SELECT COALESCE(MAX(position),0) AS position FROM events WHERE session_id=?", (session_id,))["position"]
    return {"revision": revision, "project_id": project_id, "session_id": session_id, "consumer_id": consumer_id,
            "messages": {"count": count, "position": position}, "agents": {row["agent_id"]: {"count": row["count"], "instances": row["instances"]} for row in agents}}


def instance_count_sql():
    return "(SELECT COUNT(*) FROM conversation c LEFT JOIN actions a ON a.id=c.action_id WHERE c.instance_id=i.id AND c.role='assistant' AND c.position>COALESCE((SELECT position FROM instance_read_positions WHERE consumer_id=? AND instance_id=i.id),0) AND " + VISIBLE_REPLY + ")"
