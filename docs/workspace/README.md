# workspace 模块接口

真实目录解析、文件读写、Shell 和图片工具。

源码：[src/oea/workspace/](../../src/oea/workspace/)

## Python 接口

- `workspace/paths.py`：`require_local_state(path: Path)`
- `workspace/paths.py`：`default_data_dir()`
- `workspace/paths.py`：`user_home()`、`resolve_data_dir(path=None)`、`installation_paths()`
- `workspace/paths.py`：`external_path(path: str | Path)`
- `workspace/paths.py`：`safe_child(root: Path, relative: str)`
- `workspace/service.py`：`digest(data)`
- `workspace/service.py`：`WorkspacePaths`
  - `for_instance(cls, settings: Workspace, instance_id, run_id, state_dir, *, binding=None)`
  - `prepare(cls, settings: Workspace, instance_id, run_id, state_dir, *, binding=None)`
  - `as_dict(self)`
  - `resolve(self, value, *, write=False)`
- `workspace/service.py`：`schema(properties, required)`
- `workspace/service.py`：`FileWrites`
  - `async run(self, path, write)`
- `workspace/service.py`：`LocalTool`
  - `describe(self)`
  - `async invoke(self, request)`
  - `image_type(path)`
  - `files(self, args, paths)`
  - `async command(self, request, paths, settings)`

## 依赖与边界

默认用户根是 `~/.oea`，主宿主数据在 `data/`，托管运行环境在 `runtime/`，缓存和启动入口分别在 `cache/`、`bin/`。`OEA_HOME` 可显式指定一个绝对路径以隔离测试或特殊部署；默认路径不从调用方的应用缓存、工作目录或旧 `OEA_DATA_DIR` 推导。路径查询不创建目录，宿主与客户端使用同一解析函数，并保留源码、同步目录和本地磁盘边界检查。

依赖模块：contracts、security、stdio、tools。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
