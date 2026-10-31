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

## 模型按需发现

`Tools.loading` 默认 `deferred`：业务 MCP 和函数工具首先进入可检索的短目录，完整输入 Schema 不进入首轮请求。本地基础工具和运行期工具仍使用原生声明。少量每轮都用到的固定工具可显式设为 `eager`。

模型通过 `tool_guide(query="...", cursor=0, limit=10)` 搜索当前允许的名称和短描述，通过 `tool_guide(name="exact_name")` 选择一个工具。下一次模型请求才携带该工具的完整原生参数；模型仍以原工具名调用，执行、参数验证、用量和回执均使用同一 ToolGateway。已知名称可直接选择，无需先搜索。

目录保留 MCP 的服务与原始工具名，便于检索不透明的原生别名。当前阶段不再需要的参数可用 `tool_guide(name="exact_name", load=false)` 卸载；卸载不删除原动作或结果。加载和卸载都沿原 Run 回执恢复。

选择状态与事件同事务保存在 `run_tool_state`，原 `tool_guide` 回执保留；恢复核验当前契约和授权，新的 Run 重新选择。每轮目录、Schema 与实际执行取当前授权交集。Run 的允许列表列出实际工具名，发现工具只辅助读取已获准能力；它不扩大授权，也不替代必需工具的成功回执。

`tools.schema_budget_tokens` 是延迟业务 Schema 的软驻留目标，默认 `min(8192, context.input_tokens/4)`。下一次请求准备时按压力和最近使用情况卸载；刚加载的工具至少获得一次请求机会。`tool_guide(name="exact_name", pin=true)` 保护已授权工具免于自动卸载，`eager` 保持全量加载。必要的大 Schema 可以超过软目标，但完整请求必须满足硬输入边界。模型请求记录实际契约哈希，生成调用后的契约变化返回 `TOOL_CONTRACT_CHANGED`。

参数错误保留最多五项安全字段诊断，不回显原输入。执行前拒绝与工具结果同事务记录 `tool.rejected`，关联原模型响应、调用和契约；重放不重复事件，不伪造工具派发或用量。

`tools/discovery.py` 负责确定性、有界搜索，不调用模型。`RunContext.model_input` 的投影记录 available/deferred/loaded/schemas 数量；估算包含实际发送的 Schema。该机制使用普通原生工具列表更新，不要求供应商支持专有延迟字段。
