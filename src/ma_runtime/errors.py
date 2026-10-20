"""Safe, transport-independent runtime errors."""


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
        return {
            "code": self.code,
            "category": self.code.split("_")[0].lower(),
            "retryable": self.retryable,
            "safe_message": self.safe_message,
            "details": self.details,
            **self.identity,
        }


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
