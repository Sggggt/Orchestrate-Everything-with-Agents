# interfaces 模块接口

CLI、已有宿主客户端、控制 MCP 和类型化操作注册。

源码：[src/oea/interfaces/](../../src/oea/interfaces/)

## Python 接口

- `interfaces/cli.py`：`parser()`
- `interfaces/cli.py`：`request(args)`
- `interfaces/cli.py`：`main(argv=None)`
- `interfaces/launch.py`：`serve(data_dir=None, *, host="127.0.0.1", port=None, allow_commands=False)`
- `interfaces/client.py`：`PlatformClient`
  - `from_data_dir(cls, data_dir=None, **options)`
  - `async call(self, operation, params=None, *, compact=False, consumer_id=None)`
  - `async events(self, run_id, *, after=0)`
  - `async upload_artifact(self, source, *, project_id, idempotency_key, session_id=None, name=None)`
  - `async aclose(self)`
- `interfaces/mcp.py`：`create_server(data_dir=None, *, client_factory=None)`
- `interfaces/mcp.py`：`run_server(data_dir=None)`
- `interfaces/registry.py`：`OperationRegistry`
  - `register(self, name, handler, *, read_only=False)`
  - `async call(self, name, params=None, *, correlation_id=None, causation_id=None, consumer_id=None)`
  - `describe(self, name)`
  - `guide(self, area, action=None)`

## 依赖与边界

`oea serve`、`oea mcp`、`oea request` 默认使用 `~/.oea/data`；`oea paths` 只读显示实际布局。服务启动先核验保存的本机端点并复用已有宿主，离线重启沿用保存端口；连接超时或认证失败不触发另一个宿主。MCP 与管理客户端只读取连接材料，不因目录缺失创建数据库。

依赖模块：artifacts、contracts、migration、models、storage、web、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
