# 平台 Agent 交流

只处理用户已授权的协作交流。接收方使用同一平台 session 中的 thread；不能把任意名称、Codex chat ID 或另一个空间的 thread 混用。

发送前按需查询 MessageSpec，然后：

```json
{"area":"messages","action":"send","params":{"spec":{
  "session_id":"已确认的空间", "target_thread_id":"已确认的接收对话",
  "payload":{"content":"与当前任务相关的发现、问题或交接事项"},
  "idempotency_key":"稳定的单条消息键"
}}}
```

平台外部协调者不要伪造 source_run_id / source_task_id / source_thread_id 代表某个 Agent；由正在执行的 Agent 自行调用 RunContext.send_message 时，平台才验证该 run 的来源与范围。关联文件使用同空间 artifact_ids，必要时补 correlation_id / causation_id。

主动消息可在 payload 中同时放简短 Markdown 的 summary_markdown 和完整 content。前端总览展示最近三条 Agent 消息/报告，协作消息页渲染摘要；完整内容继续交给接收 Agent。无需为摘要再调用模型。平台或工具问题、请求指导转 [运维通道](operations.md)，不伪装成普通任务成功。

`messages.get {message_id}` 检查单条；`messages.list {session_id, cursor, limit:25}` 取一页。PENDING 为待应用，OFFERED 为已交付执行端，APPLIED 才表示已确认消费；投递成功不等于对方已经处理。

发送消息不会自动启动空闲 thread。若用户要求对方实际处理，发送后转任务子技能，通过该 thread 的 turn 或 tasks.submit 启动执行；不要仅因为投递了消息就擅自派发额外工作。

不要跨会话复制密钥或完整私有日志。汇报发送的对象、message_id、回执和是否已启动处理，避免重复发送同一内容。
