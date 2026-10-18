# 任务与依赖工作流

派发前按需用 `agents.capabilities {agent_id}` 确认关联 MCP/Skill。`threads.turn`、`tasks.submit` 和批次均支持 tool_policy / skill_policy，省略默认 mode=auto；none 禁用新使用，allowed 收窄范围，required 指定必要成功动作。工具标识用 mcp:server_id:tool_name 或平台工具名，Skill 用 ID。显式 required Skill 在执行前加载，工具成功回执和正文加载记录由 Runtime 核验，缺失时不能成功。策略不会扩大 Agent 的授权；不要仅因需要使用某 Skill 就读取所有技能正文。

可保存、带变量传递和画布编辑的项目工作流使用 [orchestration.md](orchestration.md) 与 `workflows` area。本文的 tasks.workflow 保留为一次性会话任务依赖接口。

先确认 session、agent、thread 的真实归属与用户目标。简单对话用 threads.turn；需要结构化 payload、依赖、资源或期限时查询并调用 tasks.submit。

同一 Agent 的多个独立输入需要新上下文时，用[多实例编排](instances.md) 的 instances.spawn 原子创建线程与任务。已有线程上的依赖继续用本流程；不要为每个实例复制 Agent 配置。

```json
{"area":"tasks","action":"submit","params":{"spec":{
  "session_id":"session_id", "agent_id":"agent_id", "thread_id":"thread_id",
  "payload":{"input":"明确的工作目标"}, "idempotency_key":"一次逻辑提交的稳定唯一键"
}}}
```

不要把示例 ID 当成真实 ID。依赖、资源锁、期限、超时和重试只在业务需要时配置；字段以该动作的 TaskSpec 为准。

## 依赖编排

`tasks.workflow {session_id, nodes, idempotency_key}` 一次提交用户要求的任务 DAG。每个 node 有唯一 `key`、真实 `thread_id`、`payload`，`depends_on` 使用前置节点 key 或已知 task ID。agent_id 可由 thread 推导。默认 ALL_SUCCEEDED；只有确实需要清理或汇总失败结果时才使用 ALL_TERMINAL。

同一个 thread 同时只有一个执行者。要并行执行独立上下文，使用不同 thread，并尊重全局与每 Agent 的并发预算。Agent 内部代码节点不必转换成平台任务。

## 观察与处置

平台内模型主 Agent 的 wait_for_tasks 默认 ALL_SETTLED，带回成功、失败、取消及 UNCERTAIN 的状态/错误，交由主 Agent 判断。原 ALL_SUCCEEDED/ALL_TERMINAL 可显式选择。子 Agent 请求指导和双向异常反馈使用 [运维通道](operations.md)，不要靠等待失败把主决策者直接结束。

- `tasks.get {task_id}` 查看单个任务；`tasks.list {session_id, cursor, limit:25}` 取一页。
- `tasks.progress {task_id}` 查看实时或最终 Markdown 摘要；`control.live {session_id}` 查看当前执行摘要。需要完整交接内容时取 result，不把前端摘要当成 Agent 的完整消息。
- `tasks.result {task_id, timeout:0}` 非阻塞检查结果；需要等候时设不超过 30 秒。WAIT_TIMEOUT 表示任务继续，不是失败，也不应自动取消。
- queue_reason 为 MODEL_CONFIGURATION 时停止轮询，保留 task ID 并提醒用户去前端配置模型；保存后任务自动继续，不重试或重建同一个任务。
- `tasks.cancel {task_id}` 请求取消，随后检查是否确认停止；取消不会撤销已发生的外部动作。
- `tasks.resume {task_id, input:{...}, idempotency_key}` 为等待输入的任务提供恢复数据；没有真实输入时不伪造继续。
- `tasks.retry {task_id, idempotency_key}` 在失败原因已处理且重试安全时显式创建重试任务，保留旧记录。
- `tasks.reconcile {task_id, resolution:{...}}` 仅用于 UNCERTAIN，先查询这个动作的 Reconciliation 协议，再填入真实停止证据、外部动作确认及结果/重试判断。

轮询按任务耗时退避；没有状态变化时不重新获取全量历史。跨阶段保留 task ID 和最后状态。没有观察到成功结果时不能把 QUEUED、RUNNING、WAITING 或“已接收”报告为完成。

UNCERTAIN 不是一般失败。没有充分证据时说明缺失项，保留隔离状态；不得随手把 confirmed_stopped、external_actions_confirmed、safe_to_retry 全设为 true。
