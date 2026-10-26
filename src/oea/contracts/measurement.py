"""Shared measurement and content-view facts; unknown values stay explicit."""

from typing import Literal, TypedDict

Basis = Literal["measured", "tokenizer_estimate", "heuristic_estimate", "provider_reported", "unknown"]
POLICY_REVISION = "measurement-v1"
ESTIMATE_METHOD = "utf8-div3-image2048-v1"


class Measurement(TypedDict):
    metric: str
    value: int | None
    unit: str
    basis: Basis


def measurement(metric, value, unit, basis, *, limit=None, limit_source=None, decision=None):
    result = {"metric": metric, "value": value, "unit": unit, "basis": basis}
    if limit_source is not None:
        result.update(limit=limit, limit_source=limit_source, policy_revision=POLICY_REVISION,
                      decision=decision or ("unknown" if limit is None or value is None else "allow" if value <= limit else "reject"))
    return result


def _estimate(value):
    if isinstance(value, str):
        return (len(value.encode("utf-8")) + 2) // 3, 0, 0
    if isinstance(value, dict):
        if value.get("type") == "oea_image":
            text, images, framing = _estimate({"type": "text", "text": "Assigned image evidence: " + value["artifact_id"]})
            return text, images + 2048, framing
        if value.get("type") == "image_url":
            return 0, 2048, 0
        parts = (_estimate(item) for pair in value.items() for item in pair)
        framing = 4
    elif isinstance(value, (list, tuple)):
        parts = (_estimate(item) for item in value)
        framing = 2
    else:
        return 0, 0, 2
    text, images = 0, 0
    for a, b, c in parts:
        text += a
        images += b
        framing += c
    return text, images, framing


def estimated_tokens(value):
    """Local UTF-8 heuristic, not a tokenizer count or a provider guarantee."""
    return sum(_estimate(value))


def estimate_input(messages, tools):
    text, images, framing = _estimate(messages)
    schemas = estimated_tokens(tools)
    total = text + images + framing + schemas
    return {"estimated": True, "input_tokens": total, "basis": "heuristic_estimate", "method": ESTIMATE_METHOD,
            "components": {"message_text": text, "images": images, "framing": framing, "tool_schemas": schemas},
            "measurement": measurement("model_input_tokens", total, "token", "heuristic_estimate")}


def content_view(view, *, integrity="complete", source_ref=None, source_complete=True,
                 offset=0, length=None, offset_unit="unicode_codepoint", next_offset=None):
    return {"view": view, "integrity": integrity, "source_ref": source_ref, "source_complete": source_complete,
            "range": {"offset": offset, "length": length, "unit": offset_unit}, "next_offset": next_offset}


def content_integrity(value):
    """Read declared capture facts without inferring completion from prose."""
    if isinstance(value, dict):
        view = value.get("content_view")
        if isinstance(view, dict) and "integrity" in view and "source_complete" in view:
            return view["integrity"], view["source_complete"]
        refs = value.get("output_refs")
        if value.get("truncated") or isinstance(refs, list) and any(isinstance(item, dict) and item.get("truncated") for item in refs):
            return "truncated", False
    return "complete", True
