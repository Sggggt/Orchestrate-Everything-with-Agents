# collaboration 模块接口

消息、Agent协作、manager委派和持久依赖等待。

源码：[src/oea/collaboration/](../../src/oea/collaboration/)

## Python 接口

- `collaboration/coordination.py`：`Coordination`
  - `actor(db, run_id)`
  - `related(db, actor, run_id, *, cancel=False)`
  - `check(self, db, run_id, name, args)`
  - `async execute(self, run_id, name, args, *, action_id)`
- `collaboration/coordination.py`：`CoordinationTool`
  - `describe(self)`
  - `async authorize_replay(self, run_id, args)`
  - `async invoke(self, request)`
- `collaboration/dependencies.py`：`RunDependencies`
  - `watch(self, run_id, wait)`
  - `async open(self)`
  - `async aclose(self)`
- `collaboration/messages.py`：`Messages`
  - `source(db, run_id, session)`
  - `async send(self, spec: MessageSpec)`
  - `async send_from(self, run_id, spec, *, action_id=None)`
  - `async get(self, message_id: str, *, project_id: str)`
  - `async list(self, session_id: str, *, project_id: str, after: int=0, limit: int=25, topic_id: str | None=None, order: Literal['asc', 'desc']='asc', before: int | None=None)`
  - `async mark_read(self, session_id: str, consumer_id: str, position: int, *, project_id: str)`
  - `async unread(self, session_id: str, consumer_id: str, *, project_id: str)`

## 依赖与边界

依赖模块：catalog、context、contracts、storage、tools、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

`messages.list` 每项返回 `recipient_instance_ids`，与 `delivery_summary` 在同一次分页查询中读取。该列表保留接受时冻结的实际投递对象，后续新增实例不改变历史收件集合。原始选择器不作为历史推断依据。

消息来源使用 leader、manager、instance 三种正式角色；导入历史保持 import 标记。source.name 保存接受时的显示名称：Web 使用 leader，MCP 读取接入客户端的 clientInfo.name，SDK 可通过 PlatformClient(agent_name=...) 提供名称。名称只用于显示，不改变权限；同键重读保持原消息来源。旧消息未记录名称时显示角色或已有实例名称，不推断外部 Agent 身份。
