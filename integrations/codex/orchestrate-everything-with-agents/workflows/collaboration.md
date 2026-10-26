# 消息与manager协作

1. 固定 session/project，读取实际 Agent 和 instance。session 不是 Codex 聊天。
2. `messages.send` 选择 inform/request/reply/report。接收者使用真实实例 ID、department:AgentID 或 all；接收集合在接受时冻结。正文文本的 @ 不解释为执行授权。
3. 新 request 明确工作归属和成本预算；相关工作使用原 root。reply 指向原等待的 target_run_id 或 reply_to。inform/report 不新建模型推理或独立预算。
4. 协作模式经 `sessions.set_mode` 和 expected_epoch 显式切换。direct 不启动manager；collaborative 要求 manager 独立模型和配置。实例继承保存的 main 能力。
5. manager通过限定的创建实例、派发、检查、等待、发送和取消工具组织本Agent；跨Agent访问必须符合当前通信授权。manager无业务命令和管理员客户端。
6. 读取完整结果和产物引用，再做汇总。共享 context_refs 仅授权读取资料，不授予来源工具执行权。已撤销授权不因旧消息重放恢复。
7. 根预算包含manager、实例、摘要、恢复和消息处理。manager短回合结束不自动取消实例；根取消沿真实 ownership 传播。切回 direct 不丢已接受工作。

用 messages.mark_read 维护当前 consumer 的独立阅读游标；已读不代表 Agent 已应用或完成。查看 deliveries 的 accepted/started/applied/completed 事实。

正式角色名称使用 manager、leader、instance。外部来源名称由接入客户端上报并保存在消息 source.name 中；它只是归属信息，不授予权限。
