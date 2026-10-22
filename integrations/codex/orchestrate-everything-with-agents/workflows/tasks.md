# 任务与依赖工作流

派发前按需用 `admission.describe {agent_id}` 看配置/声明/观测；已有线程的 TaskSpec 可用 `admission.check {spec,budget_id?}` 无副作用预检。它不探测上游，unknown 不能当成可用证明。`threads.turn`、`tasks.submit` 和批次均支持 tool_policy / skill_policy，省略默认 mode=auto；none 禁用新使用，allowed 收窄范围，required 指定必要成功动作。工具标识用 mcp:server_id:tool_name 或平台工具名，Skill 用 ID。显式 required Skill 在执行前加载，工具成功回执和正文加载记录由 Runtime 核验，缺失时不能成功。策略不会扩大 Agent 的授权；不要仅因需要使用某 Skill 就读取所有技能正文。

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

普通任务自动有累计预算；`budgets.for_task` 查看用量，entries 分页查账。显式共享总量时先 `budgets.create`，提交时传 budget_id。摘要、继续、子任务和修复共用原账本；有效证据才重置停滞计数。PROGRESS_STALLED / BUDGET_EXHAUSTED 保留部分结果，先定位缺项，不靠重复续写或新实例绕过。

## 依赖编排

`tasks.workflow {session_id, nodes, idempotency_key}` 一次提交用户要求的任务 DAG。每个 node 有唯一 `key`、真实 `thread_id`、`payload`，`depends_on` 使用前置节点 key 或已知 task ID。agent_id 可由 thread 推导。默认 ALL_SUCCEEDED；只有确实需要清理或汇总失败结果时才使用 ALL_TERMINAL。

同一个 thread 同时只有一个执行者。要并行执行独立上下文，使用不同 thread，并尊重全局与每 Agent 的并发预算。Agent 内部代码节点不必转换成平台任务。

## 观察与处置

外部主 Agent 收集每项任务的结果或失败状态，再决定下一步。执行端的 wait_for_tasks 默认 ALL_SETTLED，带回成功、失败、取消及 UNCERTAIN 的状态/错误；原 ALL_SUCCEEDED/ALL_TERMINAL 可显式选择。子 Agent 请求指导和双向异常反馈使用 [运维通道](operations.md)。

- `tasks.get {task_id}` 查看单个任务；`tasks.list {session_id, cursor, limit:25}` 取一页。
- 派发时用 `result_policy` 声明需要的正文、结构化字段、覆盖项或产物。按当前动作查询字段，不复制整套 schema。平台检查交付条件，内容是否正确仍由外部主 Agent 核验。
- `tasks.progress {task_id}` 查看实时或最终 Markdown 摘要；`control.live {session_id}` 查看当前执行摘要。需要完整交接内容时取 result，不把前端摘要当成 Agent 的完整消息。
- `tasks.result {task_id, timeout:0}` 非阻塞检查结果；需要等候时设不超过 30 秒。WAIT_TIMEOUT 表示任务继续，不是失败，也不应自动取消。
- queue_reason 为 MODEL_CONFIGURATION 时停止轮询，保留 task ID 并提醒用户去前端配置模型；保存后任务自动继续，不重试或重建同一个任务。
- `tasks.cancel {task_id}` 请求取消，随后检查是否确认停止；取消不会撤销已发生的外部动作。
- `tasks.resume {task_id, input:{...}, idempotency_key}` 为等待输入的任务提供恢复数据；没有真实输入时不伪造继续。
- `tasks.retry {task_id, idempotency_key}` 在失败原因已处理且重试安全时显式创建重试任务，保留旧记录。
- `tasks.reconcile {task_id, resolution:{...}}` 仅用于 UNCERTAIN，先查询这个动作的 Reconciliation 协议，再填入真实停止证据、外部动作确认及结果/重试判断。

轮询按任务耗时退避；没有状态变化时不重新获取全量历史。跨阶段保留 task ID 和最后状态。没有观察到成功结果时不能把 QUEUED、RUNNING、WAITING 或“已接收”报告为完成。

UNCERTAIN 不是一般失败。没有充分证据时说明缺失项，保留隔离状态；不得随手把 confirmed_stopped、external_actions_confirmed、safe_to_retry 全设为 true。

## 交付失败与上下文交接

空正文、截断、缺少产物或覆盖项会保留部分结果，并标为 FAILED。先读 `tasks.get` 的 result/error/metadata.acceptance；定位耗时或故障阶段才读 `tasks.trace`，按 cursor 翻页，工具原始回执只读所需调用。不要根据“连接失败”猜测服务责任，也不要把通过交付检查当成审查正确。

若原线程未前进且有已提交检查点，可显式 `tasks.continue_result {task_id,idempotency_key,content}` 修补独立任务。沿用同键核对回执；后端限制次数、所有追加模型调用（含摘要）、输出和时间，并阻止新的外部动作。默认无需额外配置；确有需要时在原任务 `continuation_policy` 中收窄。预算耗尽或不满足条件时停止续写，说明剩余缺口。已终态工作流用 `workflows.repair` 创建新修复运行并保留来源，不改旧结果。

大资料和依赖保留原文，模型只接收当前工作集。独立任务交接用必要的 `context_refs`，需要原文再 `contexts.read {reference,project_id,offset,limit}`；沿 next_offset 读取需要的片段，沿 previous_reference 回溯历史。引用是版本快照且不授予额外工具权限。长审查按具体段落/问题派发，要求证据位置与明确交付项，避免多个 Agent 各自重读全量材料。

执行端的 `read_context` 是固有读取口，不填入业务工具的 allowed/required；仍须保留引用来源工具的授权，例如文件证据所需的 read_file。offset/limit 按 UTF-8 字节计。
