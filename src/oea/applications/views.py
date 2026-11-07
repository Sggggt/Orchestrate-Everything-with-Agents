"""Application-visible receipts exclude private instructions and host environment metadata."""

import re

from oea.contracts.errors import RuntimeFault, diagnostic


def public_error(error):
    value = error.as_dict() if isinstance(error, RuntimeFault) else error
    message = value.get("safe_message") if isinstance(value.get("safe_message"), str) else "Application operation failed"
    details = value.get("details") if isinstance(value.get("details"), dict) else {}
    if re.search(r"[A-Za-z]:[\\/]|https?://|/(?:home|Users|tmp|var|etc)/", message):
        message = "Application operation failed; inspect its original receipt before continuing"
    result = {"code": value["code"], "safe_message": message, "retryable": value.get("retryable", False),
              **{key: value[key] for key in ("origin", "stage", "purpose", "reason", "continuation", "action_id", "attempt_id") if value.get(key) is not None},
              "details": {key: details[key] for key in ("launch_accepted", "process_stopped", "exit_code", "state", "send_started", "provider_accepted", "response_received") if key in details}}
    facts = details.get("attempt_facts")
    if isinstance(facts, dict):
        result["details"]["attempt_facts"] = {key: facts[key] for key in ("attempt_id", "send_started", "provider_accepted", "response_received", "generation_completed", "integrity", "phase") if key in facts}
    result["diagnostic"] = diagnostic(result)
    return result


def public_status(value):
    if value.get("error"):
        value = {**value, "error": public_error(value["error"])}
    if isinstance(value.get("wait"), dict) and isinstance(value["wait"].get("reason"), dict) and value["wait"]["reason"].get("code"):
        value = {**value, "wait": {**value["wait"], "reason": public_error(value["wait"]["reason"])}}
    return value


def tool_result(name, value):
    if name in {"exec_command", "write_stdin", "apply_patch"} and isinstance(value, dict):
        value = {key: item for key, item in value.items() if key not in {"environment", "cwd"}}
        if isinstance(value.get("error"), dict) and isinstance(value["error"].get("code"), str):
            value["error"] = public_error(value["error"])
    return value
