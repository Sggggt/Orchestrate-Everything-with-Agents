# 注册与更新执行端

先用 `agents.list` / `agents.describe` 确定 ID 是否存在。新建与覆盖已有 Agent 的业务含义不同，不能为了绕开冲突擅自覆盖或生成另一个同义 ID。

新 process Agent 的最小配置形状：

```json
{
  "area":"agents", "action":"configure",
  "params":{"config":{
    "id":"business-reviewer", "name":"业务复核", "kind":"process",
    "command":["PYTHON_ABSOLUTE_PATH","-B","AGENT_FILE_ABSOLUTE_PATH"],
    "agent_md":"该执行端的职责、输入输出和边界",
    "skills":[], "mcp_bindings":[]
  }}
}
```

示例路径是格式说明，实际调用必须换成已验证的本机路径。先查 `platform_guide("agents","configure")` 了解当前协议；复杂文档或授权分阶段使用对应子技能，不一次填充所有可选字段。

1. 确认解释器、代码、依赖和最小协议测试可运行。模型凭据不从 Codex 账户推断；用户未配置时说明这项限制。
2. 填入实际实现的 AGENT.md、Skills、MCP 工具绑定；授权只含业务所需服务和工具。
   独立执行端通过 SDK 调用模型时必须设置 `llm_required=true`；确定性处理保持 false。模型 API 由用户在前端配置，不编进 command 或源码。
3. 新建调用 configure；已有执行端通常 patch `command` 等变更字段。命令路径变更不自动保证旧 checkpoint 兼容。
4. 读取注册结果验证 ID、kind、command、文档和绑定。平台保存配置时不一定已启动子进程，不能把“保存成功”当成“可执行”。
5. 读取 `model_setup`。待配置模型时完成注册并提醒前端配置，跳过真实模型验收，不无限等待；配置已具备时，按用户已授权范围建立或复用验收 session/thread，提交一个可观察结果的任务并检查结果。明确区分离线协议通过、真实接口通过和仍缺少凭据。

交付实际 Agent ID、源码位置、配置入口、验收 task ID 与结果。注册失败先检查结构与进程日志，避免反复新建 Agent。生产中更新后回滚应恢复已知版本的代码和配置，不能回写旧状态库覆盖新任务。

需要模型的 Agent，交付中必须包含配置提醒。例如：“Agent 已开发并注册，请到 Agent 集成 → 配置 → 模型 API 填写 API 地址、模型及所需密钥，保存后测试连接。本次已完成代码和离线验证，真实模型与业务验收待配置后进行。”已有可用配置时说明同一修改入口及实际验收结果，不要求重复提供密钥。
