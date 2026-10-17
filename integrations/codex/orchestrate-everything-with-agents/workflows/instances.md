# 多实例子 Agent 编排

用于把独立输入并行交给同一 Agent 或同项目 Agent 池。实例复用 Agent 配置，独立保存 Thread/Task/Run；不要为每个输入重新注册 Agent。固定步骤和列表 fan-out 使用项目工作流的 map 节点；一次性动态委派使用本流程。

先固定 project/session 与实际 Agent ID。按需查询 `platform_guide(area="instances", action="spawn")`，提交 `spec`：session_id、agent_ids、payloads（1..128 个对象）、idempotency_key；可设置 max_concurrency 与 routing（round_robin / least_loaded）。同一批次重试保持键和参数，参数改变须作为新的业务意图。Agent 池必须属于该会话项目且由平台管理；路由分配创建后固定。

- `spawn` 原子创建整个批次，返回 batch ID 与有序实例的 thread_id/task_id/agent_id。成功接收不代表完成；大响应按 result_ref 读取必要字段。
- `get {batch_id}` 读取状态、计数与成员回执；只在需要完整结果时加 include_outputs=true。`list {session_id,limit:25}` 分页读批次摘要。
- `result {batch_id,timeout:0}` 非阻塞读取全部成功结果，成员顺序与输入相同；显式等待最多 30 秒。WAIT_TIMEOUT 保留原批次，不重新 spawn。
- `cancel {batch_id}` 请求取消未完成成员；检查 confirmed_stopped 与各 Task 状态。UNCERTAIN 先核对具体 task_id，再按任务流程 reconciliation；不能把未知结果当作取消成功。
- 继续某个实例的上下文，用已有 `threads.turn`；依赖某个实例，用它的 task_id。整个批次结束状态仅汇总本次创建的任务，不包含后续对话。

全局、单 Agent、适配器声明和批次预算共同生效；BATCH_LIMIT / AGENT_LIMIT 表示正常排队。新工作区默认全局 16 槽（预留 1）、内置 Agent 4 并发，已有配置不自动改写。不要为突破预算开启另一个 Runtime。

执行端内部调用 `context.spawn_instances(spec)` 时只允许当前会话并受 collaboration 配置控制；记录返回的子 task ID，使用 suspended + WaitCondition 等待，释放父执行槽。子结果在恢复输入的 tasks 中返回。父任务失败或取消会请求取消其受管子批次；普通 Codex 管理调用不虚构 parent 身份。

失败时读成员错误和已完成结果。等待/不确定成员沿用 Task 恢复/核对契约；安全重试失败成员会创建新 Task，原批次保留原任务证据，主 Agent 单独汇总重试结果。失败工作流修复后启动新的运行。不为修复一个成员盲目重建整个批次，也不复制所有线程历史到主 Agent。
