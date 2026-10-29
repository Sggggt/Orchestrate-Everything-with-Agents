"""Reference views preserve structured status while unloading text bodies."""

import json

from oea.contracts.measurement import content_integrity, content_view
from oea.storage.persistence import encode


BODY_FIELDS = {"text", "stdout", "stderr", "content"}


def deduplicate_skills(messages):
    """Keep one visible body per immutable Skill source, retaining paired receipts."""
    reads, seen, result = set(), set(), []
    for message in messages:
        for call in message.get("tool_calls", ()):
            if call["function"]["name"] == "read_skill":
                reads.add(call["id"])
        if message["role"] == "tool" and message.get("tool_call_id") in reads:
            value = json.loads(message["content"])
            if isinstance(value, dict) and value.get("reference") and value.get("text"):
                if value["reference"] in seen:
                    value = {key: item for key, item in value.items() if key != "text"}
                    message = {**message, "content": encode({**value, "already_in_context": True})}
                else:
                    seen.add(value["reference"])
        result.append(message)
    return result


def tool_reference(value, reference):
    if not isinstance(value, dict):
        return {"content_view": content_view("excerpt", source_ref=reference)}
    result = {key: item for key, item in value.items() if key not in BODY_FIELDS or not isinstance(item, str)}
    if isinstance(value.get("structuredContent"), dict):
        # One declared protocol wrapper, not a recursive walk of arbitrary data.
        result["structuredContent"] = {key: item for key, item in value["structuredContent"].items() if key not in BODY_FIELDS or not isinstance(item, str)}
    blocks = value.get("content")
    if isinstance(blocks, list):
        retained = [item for item in blocks if not isinstance(item, dict) or item.get("type") != "text"]
        if retained:
            result["content"] = retained
        else:
            result.pop("content", None)
    integrity, complete = content_integrity(value)
    current = value.get("content_view")
    source = current.get("source_ref") if isinstance(current, dict) else None
    source = source or reference
    result["content_view"] = content_view("excerpt", integrity=integrity, source_ref=source, source_complete=complete)
    return result


def reference_message(message, row):
    reference = {"kind": "history", "id": row["id"], "after": row["position"] - 1, "read_operation": "read_history", "limit": 1}
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return message
    if message["role"] == "tool":
        try:
            value = json.loads(content)
        except ValueError:
            value = content
        body = tool_reference(value, reference)
    else:
        body = {"content_view": content_view("excerpt", source_ref=reference)}
    # Tool identities and arguments are preserved verbatim on the assistant.
    return {**message, "content": encode(body)}


def unresolved(group):
    for message in group:
        if message["role"] != "tool" or not isinstance(message.get("content"), str):
            continue
        try:
            value = json.loads(message["content"])
        except ValueError:
            continue
        if isinstance(value, dict):
            state, error = value.get("state"), value.get("error")
            if (isinstance(state, str) and state in {"intent", "dispatched", "needs_review"}
                or isinstance(error, dict) and error.get("code") == "OUTCOME_UNCERTAIN" or value.get("unresolved_actions")):
                return True
    return False
