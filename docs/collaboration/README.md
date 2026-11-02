# collaboration 模块接口

持久留言、板块读权、定向投递与明确工作协作。源码：[src/oea/collaboration/](../../src/oea/collaboration/)。

- `boards.py`：项目/会话板块、私有 inbox、显式 reader 授权及范围检查。
- `messages.py`：自由正文、原发送者、幂等保存、可选引用/提及问题、投递事实和阅读游标。
- `coordination.py`：可达成员与板块发现、获准实例创建/派发、相关结果读取、等待和沿归属取消。
- `dependencies.py`：在原 LangGraph 中断上等待真实子工作，不构造另一套推进循环。

`MessageSpec` 必需项只有 `board_id`、`content`、`idempotency_key`。有效外壳先保留正文；错误的可选成员或业务引用进入 issues，不伪造送达，也不扩大读者。普通留言不创建 Run、预算或模型调用。

角色复用相同能力类型。manager 可选；创建和派发取决于当前与快照的通信授权、项目/会话与根归属。收件、阅读和业务完成是不同事实。来源资料与消息不授予工具执行权。

公开操作为 `boards.create/get/list/set_reader`、`messages.send/get/list/mark_read/unread` 及注册的 coordination 能力。精确参数按单个操作发现，不在本文复制完整 Schema。
