"""Bounded declarative values; expressions never evaluate Python code."""

import re

from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode

TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
PATH = re.compile(r"^(inputs|nodes|item|index)(?:\.[\w-]+)*$")


def references(value):
    if isinstance(value, str):
        yield from (match.group(1).strip() for match in TOKEN.finditer(value))
    elif isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        else:
            for child in value.values():
                yield from references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from references(child)


def lookup(scope, path):
    require(isinstance(path, str) and len(path) <= 512 and PATH.fullmatch(path), "INVALID_REFERENCE", "Choose an inputs/nodes/item/index path")
    value = scope
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, (list, tuple)) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise RuntimeFault("REFERENCE_UNAVAILABLE", "Referenced value is not available", details={"path": path})
    return value


def resolve(value, scope, depth=0):
    require(depth <= 40, "INVALID_REFERENCE", "Template nesting exceeds its supported depth")
    if isinstance(value, dict):
        if "$ref" in value:
            require(set(value) <= {"$ref", "default"}, "INVALID_REFERENCE", "A reference has only $ref and optional default")
            try:
                return lookup(scope, value["$ref"])
            except RuntimeFault as error:
                if error.code == "REFERENCE_UNAVAILABLE" and "default" in value:
                    return value["default"]
                raise
        return {key: resolve(child, scope, depth + 1) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [resolve(child, scope, depth + 1) for child in value]
    if not isinstance(value, str):
        return value
    match = TOKEN.fullmatch(value)
    if match:
        return lookup(scope, match.group(1).strip())
    def replace(match):
        result = lookup(scope, match.group(1).strip())
        return result if isinstance(result, str) else encode(result)
    return TOKEN.sub(replace, value)


def compare(left, operator, right):
    if operator == "truthy":
        return bool(left)
    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    try:
        if operator == "gt":
            return left > right
        if operator == "ge":
            return left >= right
        if operator == "lt":
            return left < right
        if operator == "le":
            return left <= right
        if operator == "contains":
            return right in left
    except TypeError:
        raise RuntimeFault("CONDITION_INVALID", "Condition operands do not support this comparison") from None
    raise RuntimeFault("CONDITION_INVALID", "Condition operator is unavailable")
