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

## 最新记忆索引

阶段记忆以追加顺序确定当前版本。`memories_instance(instance_id)` 利用 SQLite 隐含 rowid 支持同实例逆序读取一条最新记忆，避免按整个实例的旧记忆排序；墙钟回拨不改变版本选择。
