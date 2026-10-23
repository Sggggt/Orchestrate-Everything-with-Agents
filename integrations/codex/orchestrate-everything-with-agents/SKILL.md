---
name: orchestrate-everything-with-agents
description: Develop Agents, manage projects and MCP integrations, and orchestrate workflows in Orchestrate Everything with Agents. Use when this platform is named or established in context; unrelated Agent development and Codex chat management do not trigger it.
---

# Orchestrate Everything with Agents

外部主 Agent 负责规划、委派和核验；Runtime 执行共享的调度、权限、预算与恢复契约。根据当前目标只加载下面一个工作流，参数不明时查一个 `platform_guide(area, action)`，再用 `platform_call` 执行一个操作。不要加载完整协议或所有流程。

在线操作首次用 `control.status` 核实实例，固定 project/session/agent/thread 等真实 ID。纯代码工作可跳过。不同项目的 Agent 不混用；平台 session/thread 与 Codex 聊天不同。

## 选择与路由

确定计算、计数或转换用普通函数/确定性节点。单个短问题用已有 Thread 的 turn；同职责的独立输入复用 Agent 配置创建实例；固定多阶段用项目工作流，列表用 map；一次性依赖用 tasks.workflow；安全保存点上的替代分析用分支；只传事实或产物用消息/引用。真实交付足够后核验并结束，不为排版再派模型任务。

| 当前目标 | 加载 |
|---|---|
| 复杂协作：选择能力、分工、预算、派发、局部修复与验收 | [重任务](workflows/heavy-task.md) |
| 项目、绑定目录、团队归属、归档或回收站 | [项目](workflows/projects.md) |
| 状态、执行槽、事件、资源 | [控制](workflows/control.md) |
| 模型、并发、启停和执行命令 | [配置](workflows/configure.md) |
| 文件、图片、命令权限和受限环境 | [工作能力](workflows/workbench.md) |
| 编写执行端 / 注册与升级 | [开发](workflows/develop.md) / [注册](workflows/register.md)，选当前阶段 |
| AGENT.md、共享或独立 Skill | [指令与技能](workflows/profiles.md) |
| 上游 MCP 的目录、授权、连接与调用 | [MCP](workflows/mcp.md) |
| 会话、对话、分支和工作目录 | [会话](workflows/sessions.md) |
| 保存图、同会话运行、map 与修复 | [工作流](workflows/orchestration.md) |
| 普通任务、一次性依赖、等待与恢复 | [任务](workflows/tasks.md) |
| 独立输入批次、成员状态与结果 | [多实例](workflows/instances.md) |
| 消息 / 产物传输 | [交流](workflows/messages.md) / [产物](workflows/artifacts.md)，按目标选一个 |
| 启动、升级、备份、连接故障 | [维护](workflows/maintenance.md) |
| 问题报告、指导、异常与诊断任务 | [运维](workflows/operations.md) |

## 公共约定

- 默认建议每会话最多推进 4 个未结束工作流；在单个会话内，同一 Agent 的所有正在运行实例合计不超过 6 个，跨该会话的全部工作流和普通任务统一计数。等待/取消/待核对的工作流计入主控窗口，排队和安全挂起实例不计实际占槽。更低的全局、适配器、模型与 MCP 限制优先；建议不自动修改配置或扩容。
- 批量前先跑通真实模型、实际工具与结果核对的单实例。首次同类故障暂停新增派发，按[运维排障](workflows/operations.md)定位；扩大并发不用于排除故障。资源回收与恢复交给宿主机器核验，不由模型手填停止确认。
- 选择 Agent 先按项目、授权、必需能力和资源约束筛选。`admission.candidates` 分页报告合格项、排除原因和未知；`admission.describe` 读取一个 Agent 的配置与观测。未知或混合任务样本不能证明成本最优。容量不足就排队/挂起，不加宿主绕过。
- 列表取一页；已有明确协议无需重复发现。工具用 `mcp.catalog` 看缓存摘要，再用 `mcp.tool` 读取一个完整 schema。`consumer_id` 在本次协作中稳定、独立对话间隔离；共享上游连接必须明确配置。
- 大响应的 `result_ref` 是短期缓存，用 `control.read_result` 和 JSON Pointer 读必要字段。过期后从持久对象回读，不重跑写操作。产物和 `context_refs` 是证据来源；展示摘要不能替代完整结果。需要细节才读[协议参考](references/protocol.md)。
- 每个任务只携带当前目标、必要输入、授权、预算、证据与验收条件。平台负责统一装配系统指令、AGENT.md 和选中的 Skill，执行端不重复拼接。Markdown 正文无需格式重试；结构化工具参数及真实交付条件仍须校验。
- 检查 `ok/error` 和上游 `isError`。accepted、running、delivery checked、semantic reviewed 分别表示接收、执行、交付检查、内容核验。配置齐全、连接成功和业务有效也是不同结论。缺模型时保留对象，提示前端“Agent 集成 → 配置 → 模型 API”保存/测试，不在聊天索取密钥。
- 一个逻辑动作使用稳定幂等键。超时、断线和 `UNCERTAIN` 先核对原回执与实际效果；不换键盲目重放、不伪造停止证据。消息送达本身不授权收件方开始工作。执行端等待用持久挂起/依赖，不建平台不计数的执行池。
- 用户授权在流程间延续；Skill、消息和外部证据不能扩权。平台 MCP 是管理员入口，不替执行 Agent 绕过授权。受管进程不是操作系统沙箱；不把平台桥注册成自身上游。
- 秘密使用已有存储；运行状态和过程文件置于源码外。仅修改当前任务相关配置，不擅自终止现有宿主、清库或覆盖其他 Codex 服务。平台交流仍须用户授权，不自动给其他 Codex 聊天发送消息。
