# 留言与按需协作

1. 固定项目和 OEA 会话，查目标板块。已认证的控制 MCP 可直接通信。平台内跨 Agent 通知或委派按[通信权限](configure.md#通信权限)配置伙伴。
2. 控制端用 `messages.send`，执行端用 `send_message`。按 Agent 发通知时填 `agent_mentions`（Agent ID），宿主投递给唯一可达实例，或多个实例中唯一可达的 manager。若当前宿主的发送动作 Schema 尚未提供该字段，按同样规则从实际成员列表确定实例。要精确投递则填 `mentions`（实例 ID），控制端取 `instances.list`、执行端取 `list_members` 的 `items[].id`。
3. 核对 `deliveries` 和 `issues`。`RECIPIENT_AMBIGUOUS` 时按任务语境确定具体实例；仍无法确定则请派发方指定。`NOT_DELIVERED` 时核对目标、会话归属、板块读权和平台内 Agent 的伙伴授权。失败的可选收件人不影响留言落盘。复核原回执再处理遗漏的通知。
4. 普通留言不创建工作、预算或模型唤醒。新工作走 runs.submit 或获准 assign_work；原等待走原 Run 的 resume。子工作等待使用持久中断和依赖。
5. manager 是可选配置，派发核验 `can_request`、当前伙伴和根归属。manager 用 `wait_for_runs` 和 `inspect_work` 取得自己派发的结果；只有任务明确要求时才另发汇报消息。
6. 交接优先传完整结果与产物引用。共享读权不等于来源工具执行权；重放旧消息不能恢复撤销的读权。
7. 使用 messages.mark_read 维护当前 consumer 的独立游标。投递、已读与业务完成分别核验。manager 回合结束不取消子工作，根取消沿实际 ownership 传播。

精确 schema 以当前单个操作为准。外部 source.name 只表示来源，不授予权限。
