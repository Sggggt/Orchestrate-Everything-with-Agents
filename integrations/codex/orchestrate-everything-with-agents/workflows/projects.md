# 项目、会话与团队

项目绑定一个已存在的本地绝对文件夹。会话和 Agent 均属于项目；切换前端项目会同时切换会话列表和 Agent 团队。绑定文件夹提供工作上下文，不自动授权任意文件写入、Shell 或业务系统操作。

按当前目标查询一个动作的 guide：

- `projects.list / get`：发现精确项目 ID、名称、文件夹和状态。
- `projects.create {name, local_folder, idempotency_key}`：用稳定键创建项目；同键不同参数拒绝。
- `projects.update {project_id, name?, local_folder?}`：更名或重新绑定文件夹。项目有未结束任务时不能更换目录。
- `projects.archive {project_id, archived:true|false}`：归档/恢复；保留历史，未结束工作需先处理。
- `projects.delete {project_id, confirmed:true}`：用户已明确授权删除该项目后，移入回收站并连同其会话一起隐藏；有未完成或待核对任务时拒绝。保留本地文件夹、Agent 配置与密钥。
- `projects.restore {project_id}`：恢复项目及随本次项目删除的会话；此前单独删除的会话不会被连带恢复。`projects.list {include_archived:true,include_deleted:true}` 可查找删除记录。
- `projects.adopt {project_id, session_ids, agent_ids}`：把明确指定的会话及对应 Agent 一起归入目标项目。必须包含相关历史会话且无未结束任务；不复制密钥、不拆散会话与其 Agent 的归属。

新建会话传 `sessions.create {project_id,title}`。创建 Agent 在 config 中填写同一个 `project_id`，或查看 `agents.list {project_id}`。存在多个开放项目时，省略项目会被拒绝；不要用失败重试或默认项目绕开。

同一 Agent ID 只属于一个项目。为另一项目创建独立 Agent，或在用户要求整体归类时使用 adopt。已有历史的 Agent 不能单独修改 project_id 造成跨项目引用。任务及消息仍限定在同一会话，项目相同并不允许自动跨会话通信。

升级旧数据时，迁移先把历史会话和 Agent 放进保留的“未分类项目”；之后按用户要求建立目标项目并 adopt。已有状态库由迁移机制自动备份，不直接修改数据库归属。
