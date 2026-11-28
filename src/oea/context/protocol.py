"""Project provider-specific continuation state without changing original history."""

import json


def project_message(row, model, run_id):
    message = json.loads(row["body_json"])
    if message.get("role") != "assistant":
        return message
    portable = {"role", "content", "tool_calls", "name"}
    calls = message.get("tool_calls", ())
    if set(message) <= portable and all(set(call) <= {"id", "type", "function"} for call in calls):
        return message
    origin = json.loads(row["model_origin"]) if row.get("model_origin") else None
    target = [model.base_url, model.model, model.credential_ref, model.thinking]
    if origin == target or origin is None and row["run_id"] == run_id:
        return message
    result = {key: value for key, value in message.items() if key in portable}
    if calls:
        result["tool_calls"] = [{key: value for key, value in call.items() if key in {"id", "type", "function"}} for call in calls]
    return result
