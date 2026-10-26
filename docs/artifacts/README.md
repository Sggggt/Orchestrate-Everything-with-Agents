# artifacts 模块接口

运行发布、用户上传、导入来源及显式文件读取授权。

源码：[src/oea/artifacts/](../../src/oea/artifacts/)

## Python 接口

- `artifacts/service.py`：`file_io(function, *args)`
- `artifacts/service.py`：`Artifacts`
  - `async publish(self, run_id, source, name, *, media_type=None, expected_sha256=None)`
  - `consumer(consumer_id)`
  - `async begin_upload(self, project_id: str, name: str, bytes: int, sha256: str, idempotency_key: str, *, session_id: str | None=None, consumer_id: str | None=None)`
  - `async get_upload(self, upload_id: str, project_id: str, *, consumer_id: str | None=None)`
  - `async write_upload(self, upload_id, project_id, chunks, *, consumer_id)`
  - `async commit_upload(self, upload_id: str, project_id: str, *, consumer_id: str | None=None)`
  - `async aclose(self)`
  - `async for_run(self, run_id: str)`
  - `async list(self, session_id: str | None, project_id: str, *, cursor: int=0, limit: int=25, query: str='')`
  - `view(row)`
  - `async get(self, artifact_id: str)`
  - `async read(self, artifact_id: str, project_id: str, *, offset: int=0, limit: int=8192)`
  - `authorize(self, db, run_id, instance, artifact_id)`
  - `assign(self, db, run_id, instance, artifact_ids, parent=None)`
  - `async authorize_for_run(self, context, artifact_id)`
  - `async image_for_run(self, run_id, artifact_id)`
  - `async read_for_run(self, context, artifact_id, *, offset=0, limit=8192)`

## 依赖与边界

依赖模块：contracts、interfaces、storage、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
