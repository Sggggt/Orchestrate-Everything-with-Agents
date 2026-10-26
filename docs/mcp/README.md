# mcp 模块接口

上游业务 MCP 的配置、发现、会话、授权和调用。

源码：[src/oea/mcp/](../../src/oea/mcp/)

## Python 接口

- `mcp/upstream.py`：`MCPServer`
  - `target(self)`
- `mcp/upstream.py`：`alias(server_id, name, revision)`
- `mcp/upstream.py`：`Connection`
  - `async serve(self)`
  - `async aclose(self)`
- `mcp/upstream.py`：`UpstreamTool`
  - `describe(self)`
  - `async invoke(self, request)`
- `mcp/upstream.py`：`UpstreamMCP`
  - `async open(self)`
  - `secret(self, reference)`
  - `async save(self, definition: MCPServer, *, expected_revision: int | None=None)`
  - `async retire_idle(self)`
  - `after_settle(self)`
  - `async remove(self, server_id: str, project_id: str, expected_revision: int)`
  - `codex_identity(name)`
  - `read_codex(config_path)`
  - `codex_definition(self, name, source, project_id)`
  - `async codex_candidates(self, project_id: str, config_path: Path, *, cursor: int=0, limit: int=25)`
  - `async import_codex(self, project_id: str, config_path: Path, server_names: tuple[str, ...])`
  - `async register_directory(self, directory: Path, project_id: str)`
  - `async inspect(self, server_id: str, project_id: str)`
  - `connection_state(self, server_id, revision)`
  - `async calls(self, server_id: str, project_id: str, *, cursor: int=0, limit: int=25)`
  - `async disconnect(self, server_id: str, project_id: str)`
  - `consumer(consumer_id)`
  - `intent_scope(db, call_id, project_id, consumer_id)`
  - `async authorize_intent(self, intent_id, name)`
  - `async call(self, server_id: str, tool_name: str, arguments: dict, idempotency_key: str, *, project_id: str, consumer_id: str | None=None, wait: bool=True)`
  - `async call_result(self, call_id: str, *, project_id: str, consumer_id: str | None=None, version: str | None=None)`
  - `async cancel_call(self, call_id: str, *, project_id: str, consumer_id: str | None=None)`
  - `async confirm_call(self, call_id: str, idempotency_key: str, evidence: str, outcome: Literal['completed', 'not_applied'], *, project_id: str, consumer_id: str | None=None, result: dict | None=None)`
  - `async get(self, server_id: str)`
  - `async list(self, project_id: str, *, cursor: int=0, limit: int=25, include_removed: bool=False)`
  - `async schema(self)`
  - `async set_credential(self, server_id: str, project_id: str, target: Literal['header', 'environment'], name: str, value: str, expected_revision: int)`
  - `async tools(self, server_id: str, project_id: str, *, revision: int | None=None)`
  - `controller_scope(project_id, consumer_id)`
  - `async connection(self, server_id, revision, *, consumer=None)`
  - `register(self, server_id, revision, catalog)`
  - `async discover(self, server_id: str, project_id: str, *, consumer_id: str | None=None)`
  - `async bind(self, context)`
  - `names(self, context)`
  - `current_grant(self, context, tool)`
  - `async authorize(self, context, name)`
  - `async aclose(self)`

## 依赖与边界

依赖模块：contracts、interfaces、storage、tools。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 管理操作

`mcp.codex_candidates(project_id, config_path, cursor=0, limit=25)` 只读取显式选择的本机 TOML，分页返回服务元数据，不返回凭据，也不建立连接。`mcp.import_codex(project_id, config_path, server_names)` 只保存选定注册项；环境与请求头的非空秘密值写入 vault，已有 ID 返回 skipped，不覆盖原配置。控制 MCP、动态认证、远程执行器和仅有工具拒绝列表的注册项需要手动配置；结果分别列出 imported、skipped 与 errors。

`mcp.remove(server_id, project_id, expected_revision)` 保存 removed=true、enabled=false；源目录、秘密、旧目录快照和动作回执保留。`mcp.list` 默认隐藏已移除项，显式 include_removed=true 可读取；get、tools 的显式旧版本和调用历史仍可读取。移除不伪造在途动作的取消事实。
