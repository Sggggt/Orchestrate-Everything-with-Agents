# stdio 模块接口

进程执行协议、工具代理、挂起恢复和受管进程树。

源码：[src/oea/stdio/](../../src/oea/stdio/)

## Python 接口

- `stdio/__init__.py`：`ProcessContext`
  - `async invoke_tool(self, name, arguments, idempotency_key)`
  - `async list_tools(self)`
  - `async instructions(self)`
  - `async complete(self, idempotency_key)`
  - `async execute_model_calls(self, response_id)`
  - `async progress(self, text)`
- `stdio/__init__.py`：`run_agent(program)`
- `stdio/agent.py`：`ProcessAgentTool`
  - `describe(self)`
  - `async invoke(self, request)`
- `stdio/process_tree.py`：`OwnedProcessTree`
  - `async stop(self)`
  - `close(self)`
- `stdio/protocol.py`：`ProgramResult`
  - `boundary(self)`

## 依赖与边界

依赖模块：contracts、storage、tools。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
