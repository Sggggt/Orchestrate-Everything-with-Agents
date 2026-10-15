# 项目内会话与 Agent 对话

`session` 是归属于项目的平台会话，`thread` 是会话内某个 Agent 的独立对话。它们不是 Codex 桌面应用的聊天；不要为平台会话调用 Codex 的 create_thread / send_message_to_thread。

先固定 project_id。用户已指定 session_id 时用 `sessions.get` 核对项目；否则按需 `sessions.list {project_id,cursor,limit:25}`。已有明确会话就复用，不默认每次创建。创建用 `sessions.create {project_id,title,metadata}`，记录返回 ID；此动作没有幂等键，响应丢失时先查列表，不盲目重复创建。

对话操作：

- `threads.create {session_id, agent_id, title}`：建立 Agent 专用对话，返回 thread_id；创建本身不运行任务。
- Agent 和会话必须属于同一项目。项目相同也不代表允许跨会话交流；归类已有历史使用 projects.adopt。
- `threads.get {thread_id}`：读取归属和状态。
- `threads.conversation {thread_id, latest:true, limit:20}`：只看近期上下文；向前翻页使用 before_seq，按返回 cursor 继续。需要增量顺序读取时使用 cursor。
- `threads.turn {thread_id, content, idempotency_key}`：提交新一轮任务，平台从 thread 推导 session / agent。
- `threads.fork {thread_id, title}`：从已有 checkpoint 分支，需执行端支持，不能强行跨框架复制上下文。

当前已知 session 中查全部 thread 可按需用 `sessions.snapshot`，只消费结果的 threads 字段；不要常规拉取所有任务、消息、产物和长对话。

关闭会话用 `sessions.close {session_id, mode:"drain"}`，等待已接收工作处理完；用户明确要求取消时使用 mode=cancel。关闭不删除历史，不停止其他会话或整个宿主。

用户已明确要删除具体会话时，使用 `sessions.delete {session_id, confirmed:true}` 移入回收站；仅开发删除按钮并不授权删除真实会话。任务排队、执行、等待输入或待核对时拒绝删除，不自动取消任务来绕过。

`sessions.restore {session_id}` 恢复原状态和历史。若项目也已删除，先恢复项目。`sessions.list {deleted_only:true}` 分页列出单独删除的会话；`include_deleted:true` 可读取全部状态。前端项目和会话都有删除按钮与确认弹窗，并提供回收站恢复入口。
