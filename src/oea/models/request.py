"""Prepare one compact UTF-8 request body and check the bytes actually sent."""

from dataclasses import dataclass
import json

from oea.contracts.configuration import MODEL_REQUEST_MAX_BYTES
from oea.contracts.errors import RuntimeFault, require
from oea.contracts.measurement import measurement


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    content: bytes
    output_tokens: int
    measurement: dict


def prepare_request(model, messages, *, tools=(), output_tokens=None):
    output = model.max_output_tokens if output_tokens is None else output_tokens
    body = {"model": model.model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}, model.token_parameter: output}
    if model.temperature is not None:
        body["temperature"] = model.temperature
    if model.reasoning_effort is not None:
        body["reasoning_effort"] = model.reasoning_effort
    if tools:
        body["tools"] = tools
    try:
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise RuntimeFault("MODEL_PROTOCOL_ERROR", "Model request must contain valid JSON", origin="local", stage="prepare",
                           details={"request_sent": False}) from None
    measured = measurement("request_body_bytes", len(content), "byte", "measured", limit=MODEL_REQUEST_MAX_BYTES,
                           limit_source="MODEL_REQUEST_MAX_BYTES")
    require(measured["decision"] == "allow", "MODEL_REQUEST_TOO_LARGE", "Encoded model input exceeds its transport bound",
            origin="local", stage="prepare", measurement=measured,
            details={"request_sent": False, "input_bytes": len(content), "max_input_bytes": MODEL_REQUEST_MAX_BYTES})
    return PreparedRequest(content, output, measured)
