# 留言与按需协作

1. 固定 project/session，按页读取实际成员及板块；session 是 OEA 会话。执行端用 `list_members`、`list_boards` 发现当前可达身份。
2. `messages.send` 提交 `MessageSpec`：board_id、自由正文和原幂等键；reply_to、mentions、上下文和产物引用按需提供。发送者与工作归属由宿主补充。
3. 检查保存、投递及 issues。错误可选提及不丢正文；私有板块读权必须显式授予，@ 不扩大读者或执行权限。
4. 普通留言不创建工作、预算或模型唤醒。新工作走 runs.submit 或获准 assign_work；原等待走原 Run 的 resume。子工作等待使用持久中断和依赖。
5. manager 是可选配置，拥有独立模型和秘密；工具、Skills、环境及执行继承定义或显式收窄。角色名称不剥夺业务工具，也不授予管理员能力。派发仍核验 can_request、当前伙伴和根归属。
6. 交接优先传完整结果与产物引用。共享读权不等于来源工具执行权；重放旧消息不能恢复撤销的读权。
7. 使用 messages.mark_read 维护当前 consumer 的独立游标。投递、已读与业务完成分别核验。manager 回合结束不取消子工作，根取消沿实际 ownership 传播。

精确 schema 以当前单个操作为准。外部 source.name 只表示来源，不授予权限。
