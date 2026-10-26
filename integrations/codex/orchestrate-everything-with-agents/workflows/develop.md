# 开发执行能力

先阅读随 Skill 分发的 [SDK 起步](../references/sdk.md)，再按目标检查当前安装版本的接口或相关源码。不要把整套协议注入模型。

1. 最小独立 Agent 使用 `oea.Agent`、Model、ToolKit、Workspace；同步 run/stream 与异步 arun/astream 共享内核，显式关闭生命周期。连接已有宿主使用 PlatformClient。执行已保存的 Agent 使用 Agent.from_saved；构造器会保存所声明的配置，不能用默认构造器替代读取。
2. 自定义普通函数用 `@tool`；参数和返回值遵守类型与统一 JSON 边界。副作用通过工具回执执行，失败参数修复保持原工作，不整任务重派。
3. 声明业务用 WorkflowBuilder；代码图注册 factory/version，并使用注入的 SQLite saver。图 state 保存 ID、键和引用；RunContext 注入临时能力，秘密、客户端和全量原文不进检查点。
4. 进程 Agent 用 `oea.stdio.run_agent`、AgentDescriptor、ProgramResult。stdout 仅 UTF-8 JSONL，日志写 stderr。声明实际支持的取消／挂起，返回 completed／suspended 边界；恢复使用显式 checkpoint，不依赖进程内存。模型通过 ProcessContext.complete 使用宿主原配置和根预算。
5. 将状态、合成输入、日志和截图放在源码外；先验证最小真实工具闭环，再增加并行与恢复。交付区分协议桩、真实模型、实际工具和内容核验。

只维护实现的能力，不自动注册演示 Agent，不给执行端管理员客户端，不以 prompt 替代授权。
