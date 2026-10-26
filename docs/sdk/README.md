# sdk 模块接口

独立 Agent 与实例的同步/异步接口、延迟初始化和生命周期。

源码：[src/oea/sdk/](../../src/oea/sdk/)

## Python 接口

- `sdk/agent.py`：`plain(value)`
- `sdk/agent.py`：`Agent`
  - `from_saved(cls, agent_id, *, state_dir=None, host=None, client=None, project_id=None, session_id=None, allow_commands=False)`
  - `instance(self, name='default')`
  - `run(self, text, **options)`
  - `async arun(self, text, **options)`
  - `stream(self, text, **options)`
  - `astream(self, text, **options)`
  - `async aclose(self)`
  - `close(self)`
- `sdk/agent.py`：`AgentInstance`
  - `async submit(self, text, *, idempotency_key=None, policy=None, budget_id=None, parent_run_id=None, artifact_ids=())`
  - `async arun(self, text, *, idempotency_key=None, timeout=None, policy=None, budget_id=None, parent_run_id=None, artifact_ids=())`
  - `run(self, text, **options)`
  - `async astream(self, text, *, idempotency_key=None, policy=None, budget_id=None, parent_run_id=None, artifact_ids=())`
  - `stream(self, text, **options)`
  - `async aresume(self, input=None, *, run_id=None, idempotency_key=None, timeout=None, refresh_model=True, refresh_capabilities=True)`
  - `resume(self, input=None, **options)`

## 依赖与边界

依赖模块：contracts、runtime、storage、tools、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。


## 生命周期与取消

构造 `Agent`、`Agent.from_saved` 和进入上下文都保持延迟初始化。首次使用才建立或连接宿主；`from_saved` 加载已有定义，不用构造默认值覆盖保存配置。新的 Run 使用共享服务接受的当前配置快照，既有实例及其工作目录继续保持稳定。

初始化失败时，SDK 关闭自己创建的 Host 并保留原异常；传入的 Host 或 `PlatformClient` 仍由调用方管理。同步使用通过 `close()` 或上下文结束关闭；异步使用通过 `await aclose()` 关闭，包括首次初始化失败的情况。同一个 Agent 始终在原事件循环中使用。

取消等待 `submit` 或 `arun` 的协程不会自动提交替代工作，也不代表服务端 Run 已停止。已发出的提交继续接收原回执，回执到达后保存在实例的 `last_run_id`；`aclose()` 等待这些提交回执，再关闭自身拥有的资源。连接共享宿主时，关闭 SDK 包装器不会关闭宿主。需要核对结果时，使用原 Run ID 调用 `runs.get` 或 `runs.result`；需要明确停止时调用共享服务的 `runs.cancel`。

提前关闭 `stream` 或 `astream` 会立即关闭底层事件观察器，不取消原 Run。流式事件与最终持久结果分别负责进度观察和结果读取，断开观察后可继续读取原回执。SDK 不会为了取消、断流或查找结果换一个键重新派发工作。


重新连接后可用 `instance.aresume(run_id=original_run_id, ...)` 恢复明确的原 Run；接受恢复后，SDK 保存该 ID，后续等待输入可继续使用同一实例的 `aresume(...)`。恢复复用原运行、预算和目录，不新建工作意图。
