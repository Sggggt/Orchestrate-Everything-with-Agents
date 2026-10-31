# storage 模块接口

SQLite 状态、事实/事件事务、结果引用和数据库 Schema。

源码：[src/oea/storage/](../../src/oea/storage/)

## Python 接口

- `storage/events.py`：`Events`
  - `async list(self, project_id: str, *, session_id: str | None=None, run_id: str | None=None, after: int=0, limit: int=100)`
  - `async stream(self, project_id, *, session_id=None, after=0)`
- `storage/persistence.py`：`new_id(prefix)`
- `storage/persistence.py`：`now_ms()`
- `storage/persistence.py`：`encode(value)`
- `storage/persistence.py`：`one(db, sql, params=())`
- `storage/persistence.py`：`rows(db, sql, params=())`
- `storage/persistence.py`：`event(db, kind, *, project_id=None, session_id=None, instance_id=None, run_id=None, intent_id=None, data=None)`
- `storage/persistence.py`：`Store`
  - `async open(self)`
  - `async call(self, function, *, transaction=False)`
  - `async close(self)`
- `storage/result_refs.py`：`plain(value)`
- `storage/result_refs.py`：`pointer(value, path)`
- `storage/result_refs.py`：`ResultReferences`
  - `source(operation, params, result)`
  - `project(db, params, result)`
  - `async issue(self, operation, params, result, consumer_id, *, characters)`
  - `async read(self, result_ref: str, *, project_id: str | None=None, consumer_id: str | None=None, json_pointer: str='', offset: int=0, limit: int=2048)`

## 依赖与边界

依赖模块：catalog、contracts、interfaces。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 稳定结果分页

动态组合响应签发引用时保存本次已返回内容的快照；分页不会重新执行来源查询。配置引用继续固定修订。引用绑定项目、consumer 和完整内容版本，读取仍核验当前归属与授权；来源删除或撤权后失败。

`results.read` 的 `limit` 为 1..2048、`offset` 为 0..2^63-1、JSON Pointer 最长 512 字符。页返回 `content_version`、选中 JSON 文本的 `content_sha256`、`characters` 和 `page_limit`；偏移单位为 Unicode 码点。等于长度返回空终页，超过长度返回 `INVALID_REQUEST`。沿原 `next_offset` 续读；最新状态须重新发起只读查询。旧无版本动态引用返回 `SOURCE_VERSION_UNAVAILABLE`，不能重跑写操作恢复。

快照与引用随所属项目清理。根 JSON 直接切页，Pointer 字段使用有界缓存；重启不改变内容，缓存不替代授权。

状态结构由 `oea upgrade-state` 离线升级并生成新备份。旧工具加载回执转换为 `run_tool_state` 候选，不补造原契约哈希；下一次投影先核验当前授权并绑定当前契约。已卸载选择保持卸载。项目删除同步清理快照、工具选择及不再被其它项目引用的工具契约。

## 最新记忆读取

阶段记忆以追加顺序确定当前版本。`memories_instance(instance_id)` 利用 SQLite 隐含 rowid 支持同实例逆序读取一条最新记忆，避免按整个实例的旧记忆排序；墙钟回拨不改变版本选择。
