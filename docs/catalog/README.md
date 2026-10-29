# catalog 模块接口

项目、Agent 配置、会话和实例的唯一管理入口。

源码：[src/oea/catalog/](../../src/oea/catalog/)

## Python 接口

- `catalog/service.py`：`need(db, table, identity)`
- `catalog/service.py`：`Projects`
  - `async create(self, name: str, *, folder: Path | None=None, project_id: str | None=None)`
  - `async get(self, project_id: str)`
  - `async update(self, project_id: str, name: str)`
  - `async delete(self, project_id: str, confirm_name: str, idempotency_key: str)`
  - `async list(self, *, cursor: int=0, limit: int=25)`
- `catalog/service.py`：`Agents`
  - `async save(self, definition: AgentDefinition, *, expected_revision: int | None=None)`
  - `async get(self, agent_id: str)`
  - `async list(self, project_id: str, *, cursor: int=0, limit: int=25)`
  - `async schema(self)`
  - `async set_credential(self, agent_id: str, project_id: str, role: str, value: str, expected_revision: int)`
  - `async check(self, agent_id: str, *, project_id: str | None=None)`
  - `async probe(self, agent_id: str, session_id: str, idempotency_key: str, *, role='main')`：接受独立模型连接探测，返回原 RunHandle；结果及用量通过 runs 查询。
  - `async remove(self, agent_id: str, project_id: str, expected_revision: int)`：删除活动登记，保留历史和文件；未结束或待核对的 Run 阻止删除。
- `catalog/service.py`：`Sessions`
  - `async create(self, project_id: str, name: str='Session', *, session_id: str | None=None)`
  - `async get(self, session_id: str)`
  - `async update(self, session_id: str, project_id: str, name: str)`
  - `async set_archived(self, session_id: str, project_id: str, archived: bool)`
  - `async summary(self, session_id: str, project_id: str)`
  - `async list(self, project_id: str, *, archived: bool | None=None, cursor: int=0, limit: int=25)`
  - `async set_mode(self, session_id: str, mode: str, expected_epoch: int)`
- `catalog/service.py`：`Instances`
  - `async create(self, session_id: str, agent_id: str, *, name: str | None=None, role: str='main')`
  - `view(row)`
  - `async get(self, instance_id: str)`
  - `async history(self, instance_id: str, project_id: str, *, before: int | None=None, limit: int=25)`
  - `async list(self, session_id: str, *, agent_id: str | None=None, cursor: int=0, limit: int=25)`

## 依赖与边界

Agent 的 `removed` 是只读生命周期元数据，通过 `agents.remove` 修改。普通列表隐藏已删除登记，`agents.list(include_removed=True)` 可核对删除结果；`agents.get` 仍可读取保留配置。普通 `save` 不复活已删除身份。重复删除可携带原修订或删除后修订；其他修订及跨项目请求拒绝。关联图的原配置不自动改写，后续使用需重新配置。

依赖模块：context、contracts、storage、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 项目与会话管理

`projects.update(project_id, name)` 只修改项目名称，不移动目录。项目仅提供永久删除，不提供归档或恢复。`projects.delete(project_id, confirm_name, idempotency_key)` 要求准确确认当前项目名，并沿用稳定请求键读取原删除回执。旧键在同 ID 项目重建后仍只返回原回执，不会删除新项目；更换确认名而复用旧键返回 `IDEMPOTENCY_CONFLICT`。项目响应不再包含旧 `archived` 字段；物理 Schema 保留该列用于已有状态的读取，但其值不再限制项目操作。

永久删除注销该项目的全部平台记录，包括 Agent 定义、会话、实例、运行、流程、消息、上下文、动作、用量、产物登记、图检查点和已导入的项目历史。其它项目、全局定义和仍被其它记录引用的共享内容保留。原始项目目录、工作目录、产物文件和离线备份保留；此操作不承诺磁盘安全擦除。混合项目的离线迁移快照清单作为备份元数据保留。

确认项目名后，删除优先于项目的旧运行和账目状态。内核撤销新工作及能力调用，取消本项目的本地执行、上传、发现和控制调用，再提交删除；`needs_review`、未结算用量及挂起记录均注销，不先伪造为成功或已结算。已发出的外部效果不保证回滚；迟到工具结果返回 `OWNER_DELETED`，不重建回执。删除聚合在一个 SQLite 事务中完成，外键始终开启；跨聚合引用阻止提交时全部回滚。删除回执包含待核对动作及用量的注销数量，不宣称其实际结算值。

关联 Agent 与 `workflow:` 线程的 LangGraph 检查点和写入一并清理。跨库清理清单暂存在原删除回执事件，清理完成后移除；中断后以原项目名和原键继续，不能换键重做业务动作。原始源文件和其它项目的检查点不受影响。

`sessions.update(session_id, project_id, name)` 修改所属项目的会话名称；`sessions.set_archived(session_id, project_id, archived)` 归档或恢复会话。会话归档保留实例、对话、运行与产物，新建实例必须先恢复，已有工作不因归档被取消。`sessions.list` 的 `archived=False` 和 `archived=True` 分别在数据库中筛选活动会话与回收站，各自保持有界游标分页；省略该参数返回全部会话。


## 配置、身份与检查

`agents.save` 保存完整六要素配置；提供 `expected_revision` 时执行修订比较，过期修订返回 `REVISION_CONFLICT`，Agent 的项目归属不能随修订变化。Agent 名称、启用状态和各项能力均通过同一配置保存入口修改。

同一会话、Agent、名称和角色的实例创建返回稳定身份，不因 Agent 改名或配置修订创建另一实例。独立名称对应独立实例；实例工作目录在执行时固定，不由管理列表操作创建。

`agents.check` 只检查已保存配置与本机可观察的准备条件，不调用模型、不连接上游 MCP、不执行工具，也不创建会话、实例、运行或目录。工作空间预览使用与实际执行相同的项目目录继承规则；继承值不写回 Agent 定义。`configuration_only` 和 `connection_verified: false` 表示尚未验证模型连接、真实任务与业务结果。

列表使用整数游标和 1 至 100 项的页面；游标必须处于 SQLite 有符号 64 位整数范围内。会话、Agent 和实例列表保留各自的项目或会话归属边界，历史读取可在归档后继续进行。

## 阅读状态

`sessions.unread(session_id, project_id, consumer_id=None)` 返回当前阅读身份的群聊未读和各 Agent 的实例未读总数、实例数及 revision。聚合覆盖整个会话，实例列表分页不限制这些总数。`sessions.summary` 在有阅读身份时携带同一 unread 投影；`instances.list` 返回逐实例 unread_count。

`instances.mark_read(instance_id, project_id, position, consumer_id=None)` 只推进该阅读身份的持久游标。位置必须属于目标实例，重复或更早位置不会回退，也不会清除其他实例或群聊的未读。实例未读计数对应含正文的 assistant 消息，不把空工具活动算作消息。首次没有游标时从零计数，不推定历史已读。

HTTP 与 MCP 的 consumer_id 由当前连接传入；显式参数不能代替另一个阅读身份。响应 revision 来自同一会话的事件位置，界面丢弃较旧的异步计数响应。
