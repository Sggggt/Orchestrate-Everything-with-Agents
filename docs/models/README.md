# API Box and model execution

API Box is a user-scoped directory of model connections. Each entry has a unique name and a stable ID; its upstream model identifier, endpoint, generation parameters, optional pricing and write-only credential are managed centrally. Agent definitions contain Box references, not inline API configuration.

Choose an Agent default during creation. Main-instance conversations can select a model for subsequent new tasks or update the Agent default. Other instances keep their explicit choices. Manager selection is available only in manager settings. An accepted Run pins its resolved configuration and version; changing a selection does not replace an active Run. Resume preserves the pinned model unless explicit refresh is requested. Disabling or revoking a connection blocks subsequent requests.

Independent summary-model references are preserved. API Box is available without a selected project. Connection probes use the existing short, isolated Run with an explicit project/session/Agent context, at most one model call and 128 output tokens. Configuration readiness, a successful connection, tool availability and business correctness are separate facts.

Model execution, usage normalization, cached-token accounting, request limits and root-budget inheritance retain their existing contracts. No new budget mechanism is introduced.

Upgrade stopped earlier state into a separate target with the offline converter. It imports all model roles, verifies references, removes legacy inline API fields from active definitions and instance configurations, and removes only unreferenced legacy credentials. Source backups and recoverable historical receipts remain intact.

Exact `models`, `agents`, `instances` and `runs` parameters are maintained by the shared typed registry.

## 请求编码边界

完整请求只进行一次紧凑 UTF-8 JSON 编码，按编码后的实际字节检查 40 MiB 上限，并将同一份字节作为 HTTP 请求正文发送，声明 `Content-Type: application/json`。Context 的编码图片总量边界为 32 MiB，请求另外留出 8 MiB 正文、工具定义及 JSON 封装空间；两者使用 contracts 中的共享常量。单图仍最多 5 MiB，原件哈希、当前授权和路径校验继续生效。图片在 Context 投影中按图像成本估算，不把 base64 字符量当作正文 token，因此传输上限需覆盖编码膨胀。

超过边界或不能编码为有效 JSON 时，在借用连接和发送请求前拒绝，错误包含 `request_sent: false`。该传输边界不修改 Context、图片授权或模型输出额度，不裁剪输入、不重置历史，也不自动重发请求。

## 流式输出边界

SSE 的行与单事件继续限制为 1 MiB。累计输出按实际保留的正文、拒绝说明、工具调用 ID、名称和参数的 UTF-8 字节计量；保留的 reasoning_content、reasoning_details 及工具签名也按字节计量；重复协议封装和未保留元数据不计入。

累计留存上限为 `max(2 MiB, 请求输出 token 上限 × 128 字节)`，随实际发出的请求额度变化；131072 token 对应 16 MiB。该上限用于有界传输和内存留存，不替代供应商的 token 计量，也不降低模型请求中的输出额度。工具参数按片段累积，在完成或失败时合并。

超限、断流、超时与协议失败保留已接受的正文、工具片段和已收到的用量。未完成工具调用保存在 `ModelFailure.partial.tool_calls` 中，由模型动作回执记录为 `discarded_tool_calls`，供核对原尝试。

普通生成在连接建立失败或响应流发生可重试的中断时，由 `ModelExecutor` 在原 Run 和预算内最多自动重连五次。收到响应头前且发送结果不明的请求进入原回执核对流程。探测和摘要各自沿用原有尝试限制。`model.reconnecting` 记录重连序号、上限和原尝试 ID。

## 准备与尝试事实

`models/request.py` 的 `prepare_request` 返回 PreparedRequest，持有唯一编码正文、输出额度与实际字节测量。请求、图片、SSE 和输出留存的数值集中在 contracts/configuration.py，维持原默认限制。

Context 的分项估算直接用于根预算核算；输入窗口按本次计划的实际输出额度扣除安全余量。事务内再核对根预算，额度缩小时更新受影响的输入边界；需要进入收尾阶段时，在发送前以收尾指引重新准备一次，不新增外部尝试。

准备、进入发送、收到 HTTP 响应和终结记录到原 action 的 JSON 元数据及同事务事件。自动重连和显式重试沿用 `action_id`，每次请求有独立的 `attempt_id` 和用量记录，上一尝试进入 `model_failures`。每个流只写阶段事实，正文增量不逐片段写数据库。自定义模型适配器未报告发送阶段时，相关事实保持未知。

captured_output_bytes 表示传输边界保留的有效内容；saved_output_bytes 表示秘密处理后实际保存的正文和工具字段。协议封装不计入这两个值。达到生成上限保留 finish_reason 并标记 incomplete；未完成工具片段只进入 discarded_tool_calls。供应商明确的结构化配额代码可分类为 quota，HTTP 403 本身不能作此判断。

HTTP 400/413 且结构化代码为 `context_length_exceeded` 或 `max_context_length_exceeded` 时，适配层记录明确拒绝并返回 `MODEL_CONTEXT_REJECTED`。默认图可凭已保存且实际减量的投影沿原 action 自动恢复一次。此恢复由图处理，传输中断由模型执行器处理。

成功及失败响应的 `input_comparison` 关联原投影版本、模型/工具指纹、预测输入、供应商实际输入与偏差；未知报告保持空值。取消仍传播取消信号，并在原回执保留已接受的正文、工具片段和用量。实时流携带 action/attempt、顺序号和 block ID；`models/stream.py` 只维护进程内活动投影及最多 16 个、合计 16 MiB 的补读快照，不能替代持久回执。

## 摘要和协议保真

`Model.thinking`、`reasoning_effort` 和生成参数只按明确模型配置发送。工具续接保留供应商需要的推理字段与不透明签名；逻辑工具组不能被摘要拆断。确定性配置错误不自动重复请求。

`summary_model_ref` 和 `ContextPolicy.summary` 分别控制摘要模型与策略；正文目标 `memory_tokens` 不充当总生成上限。最多三个有依据的 attempt，共用原预算和回执。空正文且 length 可调整额度；未知发送先核对，不换键重发。全部失败保留上一记忆与之后的持久事实/引用，硬窗口仍不足时挂起原图。
