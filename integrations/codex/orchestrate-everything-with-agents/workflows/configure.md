# Agent 配置

分配文件、图片和执行环境时加载[工作能力](workbench.md)，不在此重复工具 schema。

先确定项目和具体 Agent ID；必要时 `agents.list {project_id}`，再 `agents.describe {"agent_id":"..."}`。Agent ID `codex` 保留给外部协调者。新配置填写 project_id，已有历史归属整体移动用 projects.adopt。

已有 Agent 使用 `agents.patch {"agent_id":"...","changes":{...}}`，例如仅修改 `model`、`timeout_seconds`、`max_concurrency`、`enabled` 或 `mcp_bindings`。同一 Agent 的配置修改顺序执行；patch 是读取后合并，不是跨客户端的事务式 compare-and-swap。

创建新配置或明确替换完整配置时才使用 `agents.configure {"config":{...}}`：

- `kind=llm`：平台内置模型与工具循环。需要 `base_url`、`model`；URL 使用供应商 Chat Completions 基址，平台追加 `/chat/completions`。
- `kind=process`：独立执行程序，`command` 为参数数组，路径用绝对路径；需要模型时设 `llm_required=true`，执行端使用 `context.llm.complete`。API 地址、模型、密钥及生成参数与内置 LLM 使用同一组前端字段；注册流程见注册子技能。
- `kind=mock`：离线验证，不代表真实模型能力。
- `kind=external`：外部执行端标记，不是由平台执行的业务 Agent。

完整 configure 会覆盖省略的配置字段。描述响应中的 `descriptor`、`has_api_key`、`active_runs`、`model_setup` 等不是可写配置，不原样回传。已有 AGENT.md、skills、授权不得因仅修改模型而丢失。

`api_key` 是 configure 的独立、只写参数；省略保留原密钥。不要从配置输出寻找明文，也不要把密钥放进 AGENT.md / command。缺少密钥时优先让用户在平台设置页填写。明确删除密钥用 `agents.delete_secret`，禁用 Agent 用 patch 的 `enabled=false`。

`model_setup.state=needs_configuration` 表示模型地址或名称尚未填写；参数齐全的 configured 不代表真实验证通过。需要密钥的服务在前端填写；无需认证的本地接口允许留空。已有任务缺少模型配置时以 MODEL_CONFIGURATION 原因排队，保存配置后自动继续。

用户要求测试时用 `agents.test_model {agent_id}`，或前端“测试已保存配置”；这是一次真实的短模型请求，可能产生供应商用量。它验证连接，不代表完整业务验收。不测试尚未保存的前端草稿，不向供应商发送整个 Agent 的业务指令来做连通测试。

配置修改影响后续派发；活动执行保留快照。若更换执行框架导致 checkpoint 不兼容，为新执行创建新的 thread，保留旧历史。保存后读取目标配置核验；不会因为配置成功就声称业务运行已通过。
