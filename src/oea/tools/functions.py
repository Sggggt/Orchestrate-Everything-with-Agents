"""Typed Python functions as ordinary, run-authorized tool providers."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, get_type_hints

from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from oea.contracts.errors import RuntimeFault
from oea.contracts.schema_validation import parameter_errors
from oea.tools.contracts import ToolSpec


class FunctionTool:
    """Build the model schema and invocation binding from the same signature.

    Scalar and sequence results use a ``value`` envelope. Objects retain their
    named fields. The execution gateway owns authorization and durable receipts.
    """

    def __init__(self, function, *, name=None, description=None,
                 side_effects="none", timeout_seconds=60, cancellation="none"):
        if cancellation not in {"none", "cooperative"} or cancellation == "cooperative" and not inspect.iscoroutinefunction(function):
            raise ValueError("Function tools support cancellation='none', or 'cooperative' for coroutine functions with cancellation cleanup")
        self.function = function
        self.name = name or function.__name__
        self.description = description if description is not None else inspect.getdoc(function) or ""
        signature = inspect.signature(function)
        hints = get_type_hints(function, include_extras=True)
        fields = {}
        for parameter in signature.parameters.values():
            if parameter.kind not in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}:
                raise TypeError("Tool parameters must be named; positional-only and variadic parameters are unsupported")
            if parameter.name not in hints:
                raise TypeError(f"Tool parameter {parameter.name!r} requires a type annotation")
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[parameter.name] = (hints[parameter.name], default)
        self.arguments = create_model(self.name + "Arguments", __config__=ConfigDict(extra="forbid"), **fields)
        self.result = TypeAdapter(hints.get("return", Any))
        result_schema = self.result.json_schema()
        root_schema = result_schema
        if root_schema.get("$ref", "").startswith("#/$defs/"):
            root_schema = result_schema["$defs"][root_schema["$ref"].removeprefix("#/$defs/")]
        self.object_result = root_schema.get("type") == "object"
        value_schema = {key: value for key, value in result_schema.items() if key != "$defs"}
        output = result_schema if self.object_result else {
            "type": "object", "properties": {"value": value_schema},
            "required": ["value"], "additionalProperties": False,
            **({"$defs": result_schema["$defs"]} if "$defs" in result_schema else {}),
        }
        self.spec = ToolSpec(name=self.name, description=self.description, input_schema=self.arguments.model_json_schema(),
                             output_schema=output, side_effects=side_effects, timeout_seconds=timeout_seconds, cancellation=cancellation)

    def describe(self):
        return self.spec

    def model_schema(self):
        return {"type": "function", "function": {"name": self.name,
                "description": self.description, "parameters": self.spec.input_schema}}

    def bind_arguments(self, arguments):
        try:
            return self.arguments.model_validate(arguments)
        except ValidationError as error:
            raise RuntimeFault("INVALID_REQUEST", "Tool input does not match its declared types",
                               origin="local", stage="tool.validate", tool=self.name,
                               details={"tool": self.name, "errors": parameter_errors(error)}) from None

    async def invoke(self, request):
        arguments = request.get("bound_arguments")
        if not isinstance(arguments, self.arguments):
            arguments = self.bind_arguments(request["arguments"])
        # Keep dataclasses, enums and other typed values intact for the callable.
        values = {name: getattr(arguments, name) for name in type(arguments).model_fields}
        if inspect.iscoroutinefunction(self.function):
            result = await self.function(**values)
        else:
            result = await asyncio.to_thread(self.function, **values)
            if inspect.isawaitable(result):
                result = await result
        # Normalize once before the gateway checks the JSON result schema or
        # persists the receipt. This includes dataclasses and Pydantic models.
        normalized = self.result.dump_python(result, mode="json", warnings="error")
        return normalized if self.object_result else {"value": normalized}


def tool(function=None, **options):
    """Declare a typed tool with ``@tool`` or ``@tool(side_effects='local')``."""
    def bind(value):
        return FunctionTool(value, **options)
    return bind(function) if function is not None else bind
