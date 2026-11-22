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
            "evidence_ref": evidence, "continuation": continuation, "field_errors": details.get("errors", []),
            "available_actions": ["read_receipt"] if evidence else []}


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


def reported_tool_error(result):
    """Read bounded diagnostic fields from a tool's structured error result."""
    if result.get("isError") is not True:
        return None
    payload = result.get("structured_content", result.get("structuredContent"))
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("error"), dict):
        payload = payload["error"]
    code = payload.get("code")
    message = payload.get("safe_message") or payload.get("message") or payload.get("error")
    if not isinstance(code, str) or not 0 < len(code) <= 128 or not isinstance(message, str) or not message.strip():
        return None
    details = {field: payload[field][:512] for field in ("path", "hint") if isinstance(payload.get(field), str)}
    metadata = payload.get("details")
    if isinstance(metadata, dict):
        for field in ("feature", "database_error_code"):
            value = metadata.get(field)
            if isinstance(value, str):
                details[field] = value[:128]
            elif type(value) is int:
                details[field] = value
    if type(payload.get("retryable")) is bool:
        details["reported_retryable"] = payload["retryable"]
    if details.get("path"):
        details["errors"] = [{"path": [details["path"]], "type": code, "message": message[:512]}]
    stage = payload.get("stage")
    return RuntimeFault("TOOL_FAILED", message[:512], details=details, origin="upstream_tool", reason=code,
                        stage=stage[:128] if isinstance(stage, str) else None).as_dict()


def require(condition, code: str, message: str, **kwargs):
    if not condition:
        raise RuntimeFault(code, message, **kwargs)
