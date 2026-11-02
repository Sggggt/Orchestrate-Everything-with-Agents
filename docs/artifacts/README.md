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
  - `async candidates(self, run_id: str, project_id: str, *, source='patches', directory='', cursor=0, limit=25, version=None)`
  - `async candidate(self, reference: str, project_id: str, *, check_source=False)`

## 未发布文件候选

成功的补丁回执保留文件路径、内容哈希和大小。`artifacts.candidates` 默认分页读取这些版本；`source='outputs'` 按需观察本次 Run 的 `output_dir`，`directory` 只能选择其中的相对目录。命令创建的文件写到此范围后可从候选视图发现。其他工作文件仍可由实例中的程序读取，不因未登记为 Artifact 而被认定不存在。

输出目录按层读取，不遍历整个项目。每页最多 100 项，每次最多观察当前目录 1000 项；超过范围明确返回 `truncated`。后续页面传回 `version`，目录条目变化时重新读取第一页；这个版本不固定文件内容。链接目录不会展开。目录观察只记录大小和修改时间，内容哈希在选中文件后首次校验。有内容哈希时，视图另行标明本次 Run 是否已发布同内容副本。

候选元数据使用已有的持久来源引用保存，原观察不会被覆盖。`artifacts.candidate(..., check_source=True)` 在原快照与当前目录授权的交集内核验内容，文件变化返回 `changed` 和新的观察引用，文件缺失返回 `missing`。首次内容校验与“内容未变”分别报告；单文件内容校验上限为 50 MiB。原生环境中的枚举和校验使用与本地工具相同的 OS 隔离工作进程。

这些操作不隐式发布文件、不创建 Run、不进行业务验收。需要交付时，由明确的实例工作使用 `publish_artifact` 发布选定版本。变化提示仅要求复核引用该版本的结论，不引入逐条正文审批状态机。

## 依赖与边界

依赖模块：contracts、interfaces、storage、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
