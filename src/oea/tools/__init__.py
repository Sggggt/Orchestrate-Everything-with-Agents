"""Typed functions and local capability selection for an OEA Agent."""

from dataclasses import dataclass, replace

from oea.contracts.configuration import MCPBinding, Tools
from oea.tools.functions import FunctionTool, tool
from oea.tools.contracts import ToolProvider, ToolSpec


@dataclass(frozen=True)
class ToolKit:
    bindings: Tools
    providers: tuple[ToolProvider, ...] = ()

    @classmethod
    def local(cls, *, files=True, commands=False, images=False):
        names = ["read_file", "search_files", "write_file", "edit_file", "publish_artifact"] if files else []
        if commands:
            names.append("run_command")
        if images:
            names.append("read_image")
        return cls(Tools(local=tuple(names)))

    def with_tools(self, *functions):
        providers = tuple(value if hasattr(value, "describe") else tool(value) for value in functions)
        bindings = Tools.model_validate({**self.bindings.model_dump(),
                    "functions": self.bindings.functions + tuple(value.describe().name for value in providers)})
        return replace(self, bindings=bindings, providers=self.providers + providers)

    def with_mcp(self, server_id, *, tools=None):
        bindings = Tools.model_validate({**self.bindings.model_dump(),
                    "mcp": [*self.bindings.mcp, MCPBinding(server_id=server_id, tools=tools)]})
        return replace(self, bindings=bindings)


__all__ = ["ToolKit", "ToolSpec", "ToolProvider", "FunctionTool", "tool"]
