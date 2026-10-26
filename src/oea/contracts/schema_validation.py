"""JSON Schema validation uses bundled resources and never performs implicit I/O."""

from jsonschema import Draft202012Validator
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
