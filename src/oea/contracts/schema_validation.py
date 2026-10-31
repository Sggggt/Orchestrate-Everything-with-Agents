"""JSON Schema validation uses bundled resources and never performs implicit I/O."""

from jsonschema import Draft202012Validator
from itertools import islice
from jsonschema_specifications import REGISTRY
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from oea.contracts.errors import RuntimeFault


LOCAL_SPECIFICATIONS = REGISTRY.crawl()


def check_schema(schema):
    """Check shape and reference completeness once at declaration boundaries."""
    Draft202012Validator.check_schema(schema)
    root = DRAFT202012.create_resource(schema)
    registry = LOCAL_SPECIFICATIONS.with_resource(root.id() or "", root).crawl()
    pending = [(root, registry.resolver(root.id() or ""))]
    references, visited = [], set()
    try:
        while pending or references:
            if pending:
                resource, resolver = pending.pop()
                key = (id(resource.contents), id(resolver.lookup("").contents))
                if key in visited:
                    continue
                visited.add(key)
                if isinstance(resource.contents, dict):
                    references.extend((resolver, resource.contents[name]) for name in ("$ref", "$dynamicRef") if name in resource.contents)
                pending.extend((child, resolver.in_subresource(child)) for child in resource.subresources())
            else:
                resolver, reference = references.pop()
                target = resolver.lookup(reference)
                key = (id(target.contents), id(target.resolver.lookup("").contents))
                if key not in visited:
                    # A pointer can target a schema outside standard locations.
                    Draft202012Validator.check_schema(target.contents)
                    pending.append((DRAFT202012.create_resource(target.contents), target.resolver))
    except Unresolvable:
        raise RuntimeFault("SCHEMA_REFERENCE_UNAVAILABLE", "Bundle JSON Schema references with their definitions; implicit external retrieval is unavailable") from None


def schema_validator(schema):
    # This registry has no network or filesystem retrieval callback.
    return Draft202012Validator(schema, registry=LOCAL_SPECIFICATIONS)


def validate_value(validator, value):
    try:
        validator.validate(value)
    except Unresolvable:
        raise RuntimeFault("SCHEMA_REFERENCE_UNAVAILABLE", "JSON Schema reference is unavailable in its bundled resources") from None


def parameter_errors(error):
    """Pydantic diagnostics omit input, custom messages and validator context."""
    messages = {"missing": "Required field is missing", "extra_forbidden": "Field is not allowed",
                "greater_than_equal": "Value is below the minimum", "less_than_equal": "Value exceeds the maximum",
                "string_too_long": "Text exceeds the maximum length", "string_too_short": "Text is too short"}
    return [{"path": list(item["loc"]), "type": item["type"],
             "message": messages.get(item["type"], "Value does not satisfy the field constraint")}
            for item in islice(error.errors(include_input=False, include_url=False, include_context=False), 5)]


def schema_errors(validator, value):
    """Bounded validator traversal; never expose instance values or error text."""
    result = []
    missing_fields = set()
    def add(error):
        path = list(error.absolute_path)
        keyword = error.validator or "schema"
        if keyword == "required" and isinstance(error.instance, dict):
            location = (tuple(path), tuple(error.absolute_schema_path))
            missing = next((name for name in error.validator_value if name not in error.instance and (location, name) not in missing_fields), None)
            if missing is not None:
                missing_fields.add((location, missing))
                path.append(missing)
        messages = {"required": "Required field is missing", "additionalProperties": "Object contains fields that are not allowed",
                    "type": "Value has the wrong type", "minimum": "Value is below the minimum",
                    "maximum": "Value exceeds the maximum", "anyOf": "Value must match an allowed alternative",
                    "oneOf": "Value must match exactly one alternative"}
        result.append({"path": path, "type": keyword, "keyword": keyword,
                       "message": messages.get(keyword, "Value does not satisfy the schema constraint"),
                       "schema_path": list(error.absolute_schema_path)})
    try:
        for error in validator.iter_errors(value):
            add(error)
            if error.validator in {"anyOf", "oneOf"}:
                for branch in islice(error.context, min(2, 5 - len(result))):
                    add(branch)
            if len(result) >= 5:
                break
    except Unresolvable:
        raise RuntimeFault("SCHEMA_REFERENCE_UNAVAILABLE", "JSON Schema reference is unavailable in its bundled resources") from None
    return result


def validate_contract(validator, value, *, code="INVALID_REQUEST", stage="tool.validate", **identity):
    errors = schema_errors(validator, value)
    if errors:
        raise RuntimeFault(code, "Value does not match the declared contract", origin="local", stage=stage,
                           details={"errors": errors, "fields": [item["path"] for item in errors]}, **identity)
