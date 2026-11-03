# collaboration 模块接口

持久留言、板块读权、定向投递与明确工作协作。源码：[src/oea/collaboration/](../../src/oea/collaboration/)。

- `boards.py`：项目/会话板块、私有 inbox、显式 reader 授权及范围检查。
- `messages.py`：自由正文、原发送者、幂等保存、可选引用/提及问题、投递事实和阅读游标。
- `coordination.py`：可达成员与板块发现、获准实例创建/派发、相关结果读取、等待和沿归属取消。
- `dependencies.py`：在原 LangGraph 中断上等待真实子工作，不构造另一套推进循环。

`MessageSpec` 必需项只有 `board_id`、`content`、`idempotency_key`。有效外壳先保留正文；错误的可选成员或业务引用进入 issues，不伪造送达，也不扩大读者。普通留言不创建 Run、预算或模型调用。

角色复用相同能力类型。manager 可选；创建和派发取决于当前与快照的通信授权、项目/会话与根归属。收件、阅读和业务完成是不同事实。来源资料与消息不授予工具执行权。

公开操作为 `boards.create/get/list/set_reader`、`messages.send/get/list/mark_read/unread` 及注册的 coordination 能力。精确参数按单个操作发现，不在本文复制完整 Schema。

控制端群聊将当前会话的群聊和私聊统一按消息位置分页展示，不提供板块筛选；每条消息标明范围、实际收件人和未送达的提及。发送默认使用会话群聊板块，回复沿用原消息的板块，不因汇总展示扩大私聊读权。

`messages.list` 选择 `board_id` 或 `session_id` 其中一个范围，会话汇总页返回原板块信息及收件人名称。`messages.mark_read/unread` 使用 `board_id=None` 与 `session_id` 处理整个会话，保留按板块保存的阅读位置；批量已读不推进其它会话或其它阅读者，也不改变 Agent 的投递已读事实。执行 Agent 的 `read_messages` 仍按原板块及显式私聊读权读取。
