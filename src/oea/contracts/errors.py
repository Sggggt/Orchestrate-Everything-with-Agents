"""Safe, transport-independent runtime errors."""


def execution_state(state, error=None):
    """Project host execution independently of delivery acceptance."""
    if state == "FAILED" and error and (
        error.get("category") == "result"
        or error.get("code") in {"EMPTY_OUTPUT", "OUTPUT_TRUNCATED", "MODEL_REFUSED"}
    ):
        return "SUCCEEDED"
    return state


def diagnostic(error):
    """Project saved causes and next steps without guessing provider policy."""
    root = error
    while isinstance(root.get("cause") or root.get("details", {}).get("cause"), dict):
        root = root.get("cause") or root["details"]["cause"]
    details = root.get("details", {})
    facts = details.get("attempt_facts", {})
    evidence = root.get("evidence_ref") or error.get("evidence_ref")
    continuation = error.get("continuation") or root.get("continuation")
    return {"what": error["safe_message"], "cause": details.get("provider_message") or root.get("safe_message", error["safe_message"]),
            "origin": root.get("origin", "unknown"), "stage": root.get("stage"), "reason": root.get("reason", root.get("code")),
            "send_started": facts.get("send_started"), "response_received": facts.get("response_received"),
            "integrity": details.get("integrity", "unknown"), "measurement": root.get("measurement"),
            "evidence_ref": evidence, "continuation": continuation, "available_actions": ["read_receipt"] if evidence else []}


class RuntimeFault(Exception):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
        **identity,
    ):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.details = details or {}
        self.identity = identity

    def as_dict(self) -> dict:
        value = {
            "code": self.code,
            "category": self.code.split("_")[0].lower(),
            "retryable": self.retryable,
            "safe_message": self.safe_message,
            "details": self.details,
            "origin": "unknown", "stage": None, "purpose": None, "reason": self.code,
            "action_id": None, "attempt_id": None, "measurement": None, "evidence_ref": None,
            "continuation": None, "cause": self.details.get("cause"),
            **self.identity,
        }
        value["diagnostic"] = diagnostic(value)
        return value


class AdapterFailure(RuntimeFault):
    def __init__(
        self, message: str, *, retryable: bool = False, safe_to_retry: bool = False,
        code: str = "ADAPTER_FAILED", details: dict | None = None
    ):
        super().__init__(code, message, retryable=retryable, details=details)
        self.safe_to_retry = safe_to_retry


def require(condition, code: str, message: str, **kwargs):
    if not condition:
        raise RuntimeFault(code, message, **kwargs)
