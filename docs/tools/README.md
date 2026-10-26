# tools 模块接口

工具类型、函数绑定、授权执行、稳定动作键和回执。

源码：[src/oea/tools/](../../src/oea/tools/)

## Python 接口

- `tools/__init__.py`：`ToolKit`
  - `local(cls, *, files=True, commands=False, images=False)`
  - `with_tools(self, *functions)`
  - `with_mcp(self, server_id, *, tools=None)`
- `tools/contracts.py`：`ToolSpec`
- `tools/contracts.py`：`ToolProvider`
  - `describe(self)`
  - `async invoke(self, request: dict)`
- `tools/functions.py`：`FunctionTool`
  - `describe(self)`
  - `model_schema(self)`
  - `bind_arguments(self, arguments)`
  - `async invoke(self, request)`
- `tools/functions.py`：`tool(function=None, **options)`
- `tools/gateway.py`：`PreparedTool`
- `tools/gateway.py`：`ToolGateway`
  - `register(self, provider)`
  - `catalog(self, names)`
  - `prepare(self, name, arguments)`
  - `async invoke(self, run_id, name, arguments, operation_key)`
  - `async invoke_intent(self, intent_id, prepared, operation_key, *, wait=True)`
  - `guard(db, run_id, intent_id)`
  - `has_inflight(self, run_id)`
  - `commit_owned(self, db, action_id, run_id, result)`
  - `async confirm(self, action_id, project_id, idempotency_key, evidence, outcome, *, result=None, intent_id=None)`
  - `async inspect(self, action_id)`
  - `async cancel(self, run_id)`
  - `async aclose(self)`

## 依赖与边界

依赖模块：contracts、storage。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
