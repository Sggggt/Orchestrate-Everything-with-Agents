"""Bounded delivery checks at the shared completion boundary."""

import json
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

from .models import json_dump


@lru_cache(maxsize=64)
def output_validator(encoded):
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("Output schema exceeds 16 KiB")
    schema = json.loads(encoded)
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in node and (not isinstance(node[key], str) or not node[key].startswith("#")):
                    raise ValueError("Output schema references must stay within the schema")
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Invalid output schema") from exc
    return Draft202012Validator(schema)


def validate_result(result, policy, artifacts):
    """Return a checked result and a safe error without dropping partial delivery."""
    failures, checks = [], []
    completion = result.metadata.get("model_completion", {})
    if not isinstance(completion, dict):
        failures.append({"code": "INVALID_RESULT_METADATA", "message": "Model completion metadata must be an object"})
        completion = {}
    finish = completion.get("finish_reason")
    if completion:
        checks.append("model_completion")
        if finish in {"length", "max_tokens"}:
            failures.append({"code": "OUTPUT_TRUNCATED", "message": "Model output reached its limit"})
        elif finish in {"content_filter", "refusal"} or completion.get("refused"):
            failures.append({"code": "MODEL_REFUSED", "message": "Model did not provide the requested result"})
        if completion.get("structured_error"):
            failures.append({"code": "OUTPUT_SCHEMA_MISMATCH", "message": "Model did not return the requested JSON object"})
        if completion.get("empty") and not failures:
            failures.append({"code": "EMPTY_OUTPUT", "message": "Model did not provide nonempty result text"})
    text_required = policy.kind == "text" or (bool(completion) and policy.kind == "auto" and not artifacts)
    if text_required:
        checks.append("text")
        text = result.output.get("text")
        if not isinstance(text, str) or not text.strip():
            failures.append({"code": "EMPTY_OUTPUT", "message": "No nonempty result text was delivered"})
    if policy.kind == "structured":
        checks.append("structured")
        if not result.output:
            failures.append({"code": "EMPTY_OUTPUT", "message": "No structured result was delivered"})
    if policy.output_schema is not None:
        checks.append("output_schema")
        try:
            fault = next(output_validator(json_dump(policy.output_schema)).iter_errors(result.output), None)
        except Unresolvable:
            failures.append({"code": "INVALID_RESULT_SCHEMA", "message": "Output schema contains an unresolved local reference"})
            fault = None
        if fault is not None:
            failures.append({"code": "OUTPUT_SCHEMA_MISMATCH", "message": "Result does not match its declared schema",
                             "path": list(fault.absolute_path)})
    minimum = max(policy.min_artifacts, 1 if policy.kind == "artifacts" else 0)
    if minimum or policy.required_artifacts:
        checks.append("artifacts")
        missing = sorted(set(policy.required_artifacts) - {artifact.name for artifact in artifacts})
        if len(artifacts) < minimum or missing:
            failures.append({"code": "ARTIFACT_REQUIREMENT_UNMET", "message": "Required result artifacts are missing",
                             "missing": missing, "minimum": minimum})
    if policy.required_items:
        checks.append("coverage")
        covered = result.output.get("covered_items", [])
        missing = sorted(set(policy.required_items) - {item for item in covered if isinstance(item, str)}) if isinstance(covered, list) else list(policy.required_items)
        if missing:
            failures.append({"code": "COVERAGE_REQUIREMENT_UNMET", "message": "Required target coverage is missing", "missing": missing})
    acceptance = {"state": "FAILED" if failures else "PASSED" if checks else "NOT_REQUIRED",
                  "checks": checks, "failures": failures, "semantic_review": "UNVERIFIED"}
    checked = result.model_copy(update={"metadata": {**result.metadata, "acceptance": acceptance}})
    error = None
    if failures:
        from .presentation import delivery_summary
        checked = checked.model_copy(update={"summary_markdown": delivery_summary(result.output)})
        first = failures[0]
        error = {"code": first["code"], "category": "result", "retryable": False,
                 "safe_message": first["message"], "details": {"stage": "validation", "failures": failures}}
    return checked, error
