---
name: orchestrate-everything-with-agents
description: Develop Agents, configure capabilities, and orchestrate work in Orchestrate Everything with Agents. Use for this platform when named or established in context; unrelated Agent development and Codex chat management do not trigger it.
---

# Orchestrate Everything with Agents

leader 负责目标、计划、委派、核验和汇总；OEA Host 负责权限、成本预算、持久回执与恢复，LangGraph 驱动执行。确定性工作使用普通函数或节点，独立职责和上下文才创建 Agent。

只加载当前目标对应的一个工作流。首次在线操作用 `host.status` 核实 v2 宿主；纯源码工作可跳过。用 `platform_guide(consumer_id, area, action)` 读取一个动作，再用 `platform_call(consumer_id, operation, params)` 执行。参数和 Schema 以实际共享注册表为准。

| 当前目标 | 加载 |
| --- | --- |
| 创建项目、六要素配置、模型秘密、Skill 和工具授权 | [配置](workflows/configure.md) |
| 编写独立 Agent、自定义工具、代码图或进程 Agent | [开发](workflows/develop.md) |
| 保存业务图、map、条件、循环、子图、中断与恢复 | [编排](workflows/orchestration.md) |
| 群聊、manager 协作、跨 Agent证据与模式切换 | [协作](workflows/collaboration.md) |
| 运行、读取结果、指导、取消和原回执核对 | [控制](workflows/control.md) |
| 配置和发现上游业务 MCP | [业务 MCP](workflows/mcp.md) |
| 安装、连接、升级和状态目录 | [维护](workflows/maintenance.md) |

## 公共约定

- 六要素为模型、指令、工具、Skills、Context、Workspace／Execution。main 与 manager 独立配置模型和秘密；manager 仅拥有受限协调能力，实例继承 main 的业务能力。
- 一个会话保持稳定 `consumer_id`，独立对话使用不同值。列表按游标取一页。已有明确动作无需重复发现，不加载全部历史和完整协议。
- 长响应返回 `result_ref`，用 `results.read`、原项目和 JSON Pointer 读取所需字段。配置引用固定修订；不能为找回响应重跑写操作。详见[协议约定](references/protocol.md)。
- 实例拥有稳定目录和独立上下文，一个实例只有一个写入者。独立工作可并行，没有全局、Agent、批次、模型或 MCP 容量配额。根成本预算由 manager、实例、子图、摘要和恢复共同使用。
- `inform` 只提供资料，`request` 明确创建工作，`reply` 回到原等待，`report` 记录进展或问题。文本里的 @ 不授予权限，也不自动启动模型。已读、投递、应用、完成是不同事实。
- 一个外部意图保持原幂等键。超时或未知结果先核对原回执和真实效果；迟到结果只补事实。修复关联原 Run／图检查点，不盲目换键重派。等待使用原生挂起和依赖机制。
- accepted、实际完成、交付校验和内容正确分别核对。完整结果用于协作，摘要用于展示；资料、产物、Skill、消息和工具结果都不能扩大授权。
- 工具选择、隐藏和实际执行都受当前授权约束。共享资料读权不等于来源工具执行权。平台 MCP 是控制端入口，执行 Agent 使用 RunContext，不创建管理员客户端。
- 秘密通过本地存储和命名引用配置，不在聊天索取或输出密钥。受管进程仍具有宿主用户权限。平台控制 MCP 不注册成自身上游。
- 状态、原始数据、截图、日志与过程材料放在源码和同步目录之外。生产切换、覆盖旧状态或停止已有宿主需要明确授权；授权在同一目标中持续有效。
