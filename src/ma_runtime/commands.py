"""A single handler per typed command; domain services own durable receipts.

Commands execute directly. Durable event observers never become command
handlers, and expendable progress buffers cannot drop cancel/release commands.
"""

import inspect
from contextvars import ContextVar

from .errors import RuntimeFault, require
from .models import CommandEnvelope

command_context = ContextVar("oea_command_context", default=None)


class CommandBus:
    def __init__(self, handlers=()):
        self.handlers = {}
        self.signatures = {}
        for method, handler in dict(handlers).items():
            self.register(method, handler)

    def register(self, method, handler):
        require(method not in self.handlers, "COMMAND_HANDLER_EXISTS", "A command can have only one handler")
        self.handlers[method] = handler
        self.signatures[method] = inspect.signature(handler)

    async def execute(self, command: CommandEnvelope):
        handler = self.handlers.get(command.method)
        if handler is None:
            raise RuntimeFault("METHOD_NOT_FOUND", "Method is not available on this bridge")
        try:
            self.signatures[command.method].bind(**command.params)
        except TypeError:
            raise RuntimeFault("INVALID_REQUEST", "Method parameters do not match its contract") from None
        token = None
        if command.correlation_id or command.causation_id:
            token = command_context.set({"method": command.method, "correlation_id": command.correlation_id,
                                         "causation_id": command.causation_id})
        try:
            return await handler(**command.params)
        finally:
            if token is not None:
                command_context.reset(token)
