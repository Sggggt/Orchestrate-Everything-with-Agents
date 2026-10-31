"""Direct typed operations shared by SDK, HTTP, and MCP."""

import inspect
from contextvars import ContextVar
from typing import Any, get_type_hints

from pydantic import ConfigDict, ValidationError, create_model

from oea.contracts.errors import RuntimeFault, require
from oea.contracts.schema_validation import parameter_errors

operation_context = ContextVar("oea_operation_context", default=None)


class OperationRegistry:
    def __init__(self):
        self.handlers = {}
        self.signatures = {}
        self.parameters = {}
        self.read_only = {}

    def register(self, name, handler, *, read_only=False):
        if name in self.handlers:
            raise ValueError(f"Operation already registered: {name}")
        self.handlers[name] = handler
        self.read_only[name] = read_only
        self.signatures[name] = inspect.signature(handler)
        hints = get_type_hints(handler, include_extras=True)
        fields = {parameter.name: (hints.get(parameter.name, Any), ... if parameter.default is inspect.Parameter.empty else parameter.default)
                  for parameter in self.signatures[name].parameters.values()}
        self.parameters[name] = create_model(name.replace(".", "_") + "Parameters", __config__=ConfigDict(extra="forbid"), **fields)

    async def call(self, name, params=None, *, correlation_id=None, causation_id=None, consumer_id=None, agent_name=None):
        handler = self.handlers.get(name)
        if handler is None:
            raise RuntimeFault("METHOD_NOT_FOUND", "Operation is not available", operation=name)
        params = params if params is not None else {}
        try:
            parsed = self.parameters[name].model_validate(params)
        except ValidationError as error:
            fields = parameter_errors(error)
            raise RuntimeFault("INVALID_REQUEST", "Parameters do not match the operation contract", operation=name,
                               origin="local", stage="operation.validate",
                               details={"fields": [item["path"] for item in fields], "errors": fields}) from None
        params = {field: getattr(parsed, field) for field in type(parsed).model_fields}
        if agent_name is not None:
            require(isinstance(agent_name, str) and 0 < len(agent_name.strip()) <= 80 and all(character.isprintable() for character in agent_name),
                    "INVALID_REQUEST", "Use a bounded external Agent display name")
            agent_name = agent_name.strip()
        token = operation_context.set({"operation": name, "correlation_id": correlation_id, "causation_id": causation_id, "consumer_id": consumer_id, "agent_name": agent_name})
        try:
            return await handler(**params)
        finally:
            operation_context.reset(token)

    def describe(self, name):
        if name not in self.handlers:
            raise RuntimeFault("METHOD_NOT_FOUND", "Operation is not available", operation=name)
        return {"name": name, "description": inspect.getdoc(self.handlers[name]) or "",
                "read_only": self.read_only[name],
                "parameters_schema": self.parameters[name].model_json_schema(),
                "parameters": [{"name": p.name, "required": p.default is inspect.Parameter.empty}
                               for p in self.signatures[name].parameters.values()]}

    def guide(self, area, action=None):
        if action:
            return self.describe(area + "." + action)
        available = sorted({name.split(".", 1)[0] for name in self.handlers})
        if not area:
            return {"areas": available}
        if area not in available:
            raise RuntimeFault("METHOD_NOT_FOUND", "Capability area is not available")
        return {"area": area, "actions": [{"action": name.split(".", 1)[1], "read_only": self.read_only[name],
            "description": (inspect.getdoc(handler) or "").split("\n", 1)[0]}
            for name, handler in self.handlers.items() if name.startswith(area + ".")]}
