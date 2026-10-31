"""Evidence required to retry a definitively rejected model request."""

import hashlib

from oea.contracts.errors import RuntimeFault
from oea.storage.persistence import encode


def input_fingerprint(messages, tools, model):
    return hashlib.sha256(encode({"messages": messages, "tools": tools, "model": model}).encode("utf-8")).hexdigest()


def window_rejected(error, result):
    facts = result.get("attempt_facts", {})
    message = result.get("message", {})
    return (error.get("code") == "MODEL_CONTEXT_REJECTED" and facts.get("provider_accepted") is False
            and facts.get("response_received") is True and facts.get("terminal_at") is not None
            and facts.get("outcome") == "failed" and not message.get("content")
            and not message.get("tool_calls") and not result.get("discarded_tool_calls"))


def saved_fault(value):
    return RuntimeFault(value["code"], value["safe_message"], retryable=value.get("retryable", False),
                        details=value.get("details"), **{key: value[key] for key in
                        ("origin", "stage", "reason", "action_id", "attempt_id", "evidence_ref", "continuation") if key in value})
