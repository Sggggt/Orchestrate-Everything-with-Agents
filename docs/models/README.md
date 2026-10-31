# models 模块接口

模型传输、流式解析、请求回执和用量结算。

## 独立连接探测

`agents.probe(agent_id, session_id, idempotency_key, role="main")` 接受独立实例中的精简 Run，返回 RunHandle。通过 `runs.result` 或 `runs.inspect` 读取原结果、失败事实和用量；同键查询沿用原实例和 Run。manager 使用其自己的模型与秘密，并要求协作模式。SDK 提供 `Agent.probe()` / `await Agent.aprobe()`；Web 的主模型与 manager 设置提供“测试模型连接”。

探测仅沿用所选模型，使用短指令、最多 128 输出 token、一次模型调用和独立根预算。它不加载业务 Skill、MCP、函数工具、原件、历史或配置的执行程序，也不更改已保存的 Agent。它仍使用正式模型动作、授权与回执，不自动重试。正常业务工作继续满足原有 required 和交付契约。

`agents.check` 仍只核对配置，不产生模型费用。连接响应只证明所选模型能够响应，工具可用性和实际业务效果另行验证。输入统计区分本地估算、供应商报告、缓存命中和未决估算；不得把预算上限或失败暂记值当成实际消费。

源码：[src/oea/models/](../../src/oea/models/)

## Python 接口

- `models/api.py`：`provider_error(response, redact)`
- `models/api.py`：`ModelFailure`
- `models/api.py`：`ModelCancelled`
- `models/api.py`：`sse_data(response, max_bytes=1024 * 1024)`
- `models/api.py`：`ModelAPI`
  - `async complete(self, model, messages, *, tools=(), output_tokens=None, emit=None, on_fact=None)`
- `models/execution.py`：`ModelExecutor`
  - `async complete(self, context, key, build_input, *, purpose='model', output_tokens=None, conversation=True)`
- `models/transport.py`：`ModelTransport`
  - `async borrow(self, base_url)`
  - `async aclose(self)`

## 依赖与边界

依赖模块：context、contracts、storage。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 请求编码边界

完整请求只进行一次紧凑 UTF-8 JSON 编码，按编码后的实际字节检查 40 MiB 上限，并将同一份字节作为 HTTP 请求正文发送，声明 `Content-Type: application/json`。Context 的编码图片总量边界为 32 MiB，请求另外留出 8 MiB 正文、工具定义及 JSON 封装空间；两者使用 contracts 中的共享常量。单图仍最多 5 MiB，原件哈希、当前授权和路径校验继续生效。图片在 Context 投影中按图像成本估算，不把 base64 字符量当作正文 token，因此传输上限需覆盖编码膨胀。

超过边界或不能编码为有效 JSON 时，在借用连接和发送请求前拒绝，错误包含 `request_sent: false`。该传输边界不修改 Context、图片授权或模型输出额度，不裁剪输入、不重置历史，也不自动重发请求。

## 流式输出边界

SSE 的行与单事件继续限制为 1 MiB。累计输出按实际保留的正文、拒绝说明、工具调用 ID、名称和参数的 UTF-8 字节计量；重复 JSON 封装、供应商元数据和未保留的推理字段不占用该额度。

累计留存上限为 `max(2 MiB, 请求输出 token 上限 × 128 字节)`，随实际发出的请求额度变化；131072 token 对应 16 MiB。该上限用于有界传输和内存留存，不替代供应商的 token 计量，也不降低模型请求中的输出额度。工具参数按片段累积，在完成或失败时合并。

超限、断流、超时与协议失败保留已接受的正文、工具片段和已收到的用量。即使正文为空，未完成工具调用仍保存在 `ModelFailure.partial.tool_calls` 中并标记 `interrupted`，由模型动作回执记录为 `discarded_tool_calls`；这些片段不能作为成功工具调用执行。失败不会自动重发模型请求。

## 准备与尝试事实

`models/request.py` 的 `prepare_request` 返回 PreparedRequest，持有唯一编码正文、输出额度与实际字节测量。请求、图片、SSE 和输出留存的数值集中在 contracts/configuration.py，维持原默认限制。

Context 的分项估算直接用于根预算核算；输入窗口按本次计划的实际输出额度扣除安全余量。事务内再核对根预算，额度缩小时只更新受影响的输入边界投影，不绕过根预算或重复构造模型输入。

准备、进入发送、收到 HTTP 响应和终结记录到原 action 的 JSON 元数据及同事务事件。attempt_id 沿用原 usage ID；显式重试保留 action_id，上一尝试进入 model_failures。每个流只写阶段事实，正文增量不逐片段写数据库。自定义模型适配器未报告发送阶段时，相关事实保持未知。

captured_output_bytes 表示传输边界保留的有效内容；saved_output_bytes 表示秘密处理后实际保存的正文和工具字段。协议封装不计入这两个值。达到生成上限保留 finish_reason 并标记 incomplete；未完成工具片段只进入 discarded_tool_calls。供应商明确的结构化配额代码可分类为 quota，HTTP 403 本身不能作此判断。

HTTP 400/413 且结构化代码为 `context_length_exceeded` 或 `max_context_length_exceeded` 时，适配层记录明确拒绝并返回 `MODEL_CONTEXT_REJECTED`。其它状态、错误文本、流内错误和超时不使用此分类。默认图可凭已保存且实际减量的投影沿原 action 自动重试一次；失败尝试单独归档，模型执行器不自行循环重试。

成功及失败响应的 `input_comparison` 关联原投影版本、模型/工具指纹、预测输入、供应商实际输入与偏差；未知报告保持空值。取消仍传播取消信号，并在原回执保留已接受的正文、工具片段和用量。实时流携带 action/attempt、顺序号和 block ID；`models/stream.py` 只维护进程内活动投影及最多 16 个、合计 16 MiB 的补读快照，不能替代持久回执。
