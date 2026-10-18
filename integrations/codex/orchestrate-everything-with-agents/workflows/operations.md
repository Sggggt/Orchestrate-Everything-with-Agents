# 双向运维与指导

先固定 session。用 `operations.bind` 绑定本次对话稳定的 consumer_id；平台内主 Agent 可同时指定同会话的协调 thread。同一会话只有一个当前接收端，切换接收端是明确的配置变更。

当前阶段只发现一个动作。主动查看用 `inspect`，定向诊断用 `test`；测试返回普通 task_id，复用 tasks.get/result/cancel 等回执与恢复机制。诊断可能调用已配置模型与真实工具，按已授权目标派发，不启动另一套 Runtime、任意 Shell 或后台 Agent 池。

被动接收用 `inbox`，默认 20 条短摘要，正文只对选中的 event_id 调用 `read`。处理后 `ack` 返回游标；省略下一次 cursor 会从持久确认位置继续。需要等待时 timeout=20..25，空超时保持安静，不做高频轮询。事件可能重送，按 event_id 去重；不要因丢失工具响应重跑写操作。RESYNC_REQUIRED 时从已确认位置重新读取。

子 Agent 可通过 report_issue 报告问题或请求指导，wait=true 会挂起释放执行槽。主 Agent 以 report_id 调用 `reply`，使用稳定幂等键；已有等待在同任务/检查点中继续，指导不扩大工具权限。回复和确认收件都不等于问题解决，仍核验实际结果。内部主 Agent 收到 operations_available/pending_wait 时先处理报告，再重新等待尚未完成的子任务；ALL_SETTLED 会带回失败和 UNCERTAIN，后者先核对，不盲目重试。

前端沿用会话总览和协作消息页显示短 Markdown 反馈与报告/指导状态，没有独立运维页面；报告详情仍按 event_id 在 Agent 通道读取。文件更新与运行宿主重新加载是不同状态；新增后端能力需在已有授权下重载宿主，不能把 Skill 或 MCP 配置更新当成宿主已升级。

保持上下文轻量：不取完整会话、所有 Agent 文档或全工具 schema；保留接收端、游标、任务/报告 ID、幂等键和已验证结论即可。外部 Codex 的活动工具调用可等待更新，客户端可订阅事件；平台不能自行启动闲置外部聊天的推理回合。

Codex 的 tool_output_token_limit 可从 4000 起步，MCP 工具超时需大于 25 秒的等待窗口；常规分诊选择当前模型支持的适中推理强度，复杂修复再提高。连接现有宿主保留 --connect --compact，不修改全局权限或为了运维绕过审批。参数的现行含义见 [OpenAI 配置参考](https://developers.openai.com/codex/config-reference/)。
