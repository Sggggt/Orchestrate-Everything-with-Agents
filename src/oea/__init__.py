"""Orchestrate Everything with Agents: the public Python SDK."""

import ma_runtime as _runtime
from ma_runtime import __all__ as _runtime_exports
from ma_runtime.brand import VERSION as __version__

__all__ = [*_runtime_exports, "AgentAdapter", "__version__"]


def __getattr__(name):
    if name == "AgentAdapter":
        from agent.base import AgentAdapter
        return AgentAdapter
    if name in _runtime_exports:
        return getattr(_runtime, name)
    raise AttributeError(name)
