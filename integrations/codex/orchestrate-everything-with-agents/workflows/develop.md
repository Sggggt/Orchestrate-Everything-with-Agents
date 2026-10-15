# 开发业务 Agent

先固定业务目标、输入、输出、可用数据源和验收条件。能从上下文确定的选择自行处理；缺少业务对象或关键凭据时才询问。仅需要提示词与获准工具循环时优先配置内置 LLM Agent；有确定节点、分支、计算或原生图需求时开发 process Agent。

LLM 凭据缺失不阻塞代码开发和离线测试。只有模型能力或供应商选择影响业务设计时才询问；API 密钥留给用户在前端配置，不要求在聊天中提供。

## 代码与协议

按需读取 `development` 的单个协议：`descriptor`、`request`、`event`、`task`、`message`、`config`。先定位[本机实例](../references/local-instance.md)里的源码和 SDK 指南，只读取相关执行端与示例；不扫描整个仓库。

- 执行端实现 `describe / execute / cancel / probe / aclose`，`execute` 为异步事件流。独立进程入口调用 `oea.stdio.run_agent(adapter)`。
- 每次执行可启动新的子进程；持久状态必须通过 checkpoint、产物或框架自己的受管持久层保存，不能依赖上一个进程的内存。
- 用 `request.agent_md`、技能目录和按需 `context.read_skill`；写了文档不代表自定义代码会自动遵从，执行端必须明确消费它们。
- 需要模型的独立 Agent 注册为 `kind=process, llm_required=true`，调用 `await context.llm.complete(messages, tools=..., emit_progress=...)`，返回 `LLMResponse.text / tool_calls / usage / model`。平台负责按该 run 的前端配置调用模型，代码不硬编码 API 地址、模型或密钥，也不自己读取其他 Agent 的凭据。
- `await context.llm.info()` 返回这次执行的非秘密模型配置。complete 默认合入 system_prompt 和 AGENT.md；Skills 正文仍由执行端按需读取。需要单独控制某个节点的系统消息时可设 include_instructions=false。模型返回的工具调用不会自动执行，仍须通过获准的 RunContext 工具执行。
- 模型配置修改影响后续 run，同一 run 中的多个节点保持同一份配置快照。无需模型的确定性执行端保持 llm_required=false。协议按需查询 development.llm_request / llm_response；完整示例见 examples/model_agent.py。
- MCP 工具通过 `context.list_tools` / `context.invoke_tool`，使用返回的稳定工具别名。不要在子进程中自行读取平台 token，或创建管理员 PlatformClient 绕开该 run 的授权。
- `context.send_message`、`submit_task` 的作用域由平台验证。等待其他工作时提交 suspended 边界释放执行槽；不要自建不受平台计数的子 Agent 池。
- stdout 只承载 JSONL，日志写 stderr。最终必须产生一次 completed 或 suspended 边界；异常不能伪装成成功。
- JSONL 是 SDK 传输协议，模型正文无需 JSON。用 `TaskResult(output={"text":完整Markdown}, summary_markdown=摘要)` 分开完整协作内容与前端展示。不因 Markdown 排版差异或缺少标题进行格式修复重试。
- `context.llm.complete(..., emit_progress=True)` 流式展示摘要；建议先写任务、行动、结论，再以 `<!-- agent:details -->` 分隔完整内容。标记是可选展示约定，缺少时平台截取有界摘要；完整正文照常保留。
- `request.project_id`、`request.project_folder` 是当前会话的项目快照。不得用另一个项目的 Agent 或连接替代当前目标。
- 小型报告使用 `context.put_bytes(name, data, metadata=...)`，将返回产物 ID 放入 `TaskResult.artifact_ids`；方法见 `agent/stdio_sdk.py`，结果类型见 `src/ma_runtime/models.py`。JSONL 产物每帧最多 512 KiB，不能自行绕过大小限制。
- 能力描述必须与实现匹配。需要挂起、分支、消息回执或原生 checkpoint 时，同时实现并声明 `process_descriptor`，不要复制示例中的能力而不实现。

## 业务节点

确定的跨 Agent 步骤、分支、变量传递和文本转换可使用项目工作流：`WorkflowBuilder` / `runtime.workflows` / `client.workflows` 与前端画布共用 graph。按需切换到 [项目工作流](orchestration.md)，节点配置通过 workflows.node_types 查询。

需要任意业务代码或框架图时，将业务节点写成清楚的函数或图节点，声明输入、输出、分支条件、重试条件和终止条件。LangGraph 等框架可以在执行端内部封装；内部图不自动转换成平台画布。现有 `agent/langgraph.py` 是嵌入适配器，转换为进程执行时仍需匹配 descriptor 和上下文桥接。

平台任务 DAG 用于不同任务间依赖；Agent 内部图用于单个业务执行流程。按真实职责选择，不强制把每个函数注册为一个 Agent。

## 验证与交付

使用仓库外临时数据目录，验证输入错误、正常结果、取消及用到的权限/状态边界。离线 fixture 或 mock 模型只证明协议与实现路径，不证明真实业务数据和模型效果。真实接口验收限于用户已经授权的数据与动作。

源码、依赖声明和可公开说明留在项目；运行数据、密钥、下载和测试输出在仓库之外。完成相应离线验证后由总入口切换到注册子技能；不要把示例 Agent 静默注册进正式工作区。模型尚未配置时可以注册为待配置，但不要为了验收提交一个会一直等待模型配置的任务。

交付时必须提醒用户去“Agent 集成 → 该 Agent 的配置 → 模型 API”填写 API 地址、模型和所需密钥，再点击“测试已保存配置”，随后做真实业务验收。说明这次完成了哪些离线验证，以及哪些真实调用仍待用户配置；已有配置时也说明前端修改入口和实际验证结果。
