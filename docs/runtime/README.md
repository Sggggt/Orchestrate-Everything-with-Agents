# runtime 模块接口

宿主装配、Run 接受、权限、取消、恢复和运行期能力注入。

源码：[src/oea/runtime/](../../src/oea/runtime/)

## Python 接口

- `runtime/context.py`：`BuiltinTool`
  - `describe(self)`
  - `async check_action(self, context, action_id)`
  - `async authorize_replay(self, run_id, args)`
  - `async invoke(self, request)`
- `runtime/context.py`：`RunContext`
  - `async prepare(self, state, *, uses_model=True)`
  - `tool_names(self)`
  - `deferred_tools(self, names=None)`
  - `async load_tool(self, name, *, load=True)`
  - `async restore_tools(self)`
  - `filter_tools(self, names)`
  - `coordination_tools(self)`
  - `check_tool_policy(self, name)`
  - `async instructions(self)`
  - `available_skills(self)`
  - `skill_binding(self, skill_id)`
  - `async read_skill(self, skill_id, relative='SKILL.md')`
  - `async summarize(self, messages, output_tokens)`
  - `async call_model(self, state)`
  - `async model_input(self, output_tokens=None)`
  - `async response(self, identity)`
  - `async execute_tools(self, state)`
  - `async model_messages(self, messages)`
  - `async observe(self, state)`
  - `async apply_reply(self, state, value)`
  - `async deliver(self, state)`
  - `async complete_result(self, response_id, text, reason, *, data=None, origin='model', attempt_id=None)`
- `runtime/host.py`：`Host`
  - `assert_open(self)`
  - `async status(self)`
  - `async describe(self, operation: str)`
  - `async guide(self, area: str='', action: str | None=None)`
  - `redact(self, value)`
  - `secret(self, reference)`
  - `register_tool(self, provider)`
  - `register_graph(self, name, factory, *, version)`
  - `notify(self)`
  - `run_signal(self, run_id)`
  - `notify_run(self, run_id)`
  - `on_action_settle(self, run_id)`
  - `async authorize_intent(self, intent_id, name)`
  - `on_intent_settle(self, intent_id)`
  - `async emit_progress(self, run_id, frame)`
  - `async events(self, run_id)`
  - `async authorize_model(self, context)`
  - `async authorize(self, run_id, name, *, source_path=None)`
  - `async resolve_workspace(self, run_id)`
  - `launch(self, run_id)`
  - `async finish(self, run_id, state, *, error=None, wait=None, expected_state=None)`
  - `async aclose(self)`
- `runtime/runs.py`：`Runs`
  - `async submit(self, spec: RunSpec, *, workflow_run_id: str | None=None)`
  - `async get(self, run_id: str)`
  - `view(row)`
  - `async list(self, session_id: str, *, project_id: str, cursor: int=0, limit: int=25, instance_id: str | None=None)`
  - `async inspect(self, run_id: str)`
  - `async action(self, action_id: str, *, offset: int=0, limit: int=8192, attempt_id: str | None=None)`
  - `async attempts(self, action_id: str, project_id: str, *, cursor: int=0, limit: int=25)`
  - `async result(self, run_id: str, *, timeout: float=0)`
  - `async resume(self, run_id: str, idempotency_key: str, *, input: str | dict | None=None, refresh_model: bool=True, refresh_capabilities: bool=True, execution_limits: ResumeExecutionLimits | None=None)`
  - `async cancel(self, run_id: str)`
  - `async confirm_action(self, action_id: str, project_id: str, idempotency_key: str, evidence: str, outcome: Literal['completed', 'not_applied'], *, result: dict | None=None)`

## 依赖与边界

依赖模块：artifacts、budgets、catalog、collaboration、context、contracts、interfaces、mcp、models、orchestration、security、skills、stdio、storage、tools、workspace。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 共享入口与归档

项目和会话改名、会话归档/恢复通过 `projects.update`、`sessions.update`、`sessions.set_archived` 调用同一目录服务。归档拒绝新的工作意图，已经接受的工作与原幂等回执保留。

`Host.history` 提供 `history.summary/list/get/conversation/usage` 只读操作，读取离线迁移保存的事实；历史不会进入新模型上下文，也不会重放旧动作。MCP 服务导入与软移除通过 `mcp.codex_candidates`、`mcp.import_codex`、`mcp.remove` 复用业务服务。

## 项目生命周期

`projects.delete` 在确认名称后撤销项目的新工作与能力调用，取消所属本地任务、发现、控制调用和上传，再在领域事务中注销全部所属记录；未决状态和待核对用量不阻挡删除。宿主回收项目配置、工具校验器和隔离 MCP 连接，并清理 Agent/Workflow 的图检查点；原键支持跨库清理恢复。不能撤销的外部工作不被伪造为已停止，其迟到结果以 `OWNER_DELETED` 丢弃。其它项目及共享配置继续使用。注销范围是生命周期边界，不提供容量准入；物理工作目录及离线备份保留。

## 原回执与诊断

`runs.resume` 接受可选 `execution_limits={"max_steps": ...}`（1..100000）；省略沿用最近接受的值。该值限制每次 LangGraph 调用的 super-step，不重置原根预算。恢复键同时绑定限额变化、原失败与检查点。`EXECUTION_STEP_LIMIT` 提供实际限额、图版本及检查点；`NO_PROGRESS` 等停止原因并列保留最近阻断 `cause` 和 `evidence_ref`。

取消请求、仍在途和已确认停止分别以 `in_flight`、`stop_confirmed`、`stopped_at` 展示。旧 attempt 的迟到结果只结算其原回执，不能改写新 attempt 或复活已注销项目。

runs.action 在同一 attempt_id 上续读，返回 offset_unit、content_view 和 attempt_facts；runs.attempts 的当前与归档项使用同一事实口径。结果与错误保留稳定 action_id 和独立 attempt_id，摘要包装错误的 diagnostic 指向原始原因及回执。读取不会重发动作，恢复仍复核当前授权与状态。

`runs.response` 按原 action/attempt 读取正文，续页带首个响应的 `version`。运行中可读取进程内有界快照，终结后读取已保存事实；宿主重启后未保存部分明确标为 `unsaved_gap`。旧连接更新不能改写新 attempt。`runs.inspect.model_input` 给出最近业务模型投影的输入估算、工具 Schema 成本、加载数量、缩减前后值及供应商输入偏差。

默认图只对已确认的 `MODEL_CONTEXT_REJECTED` 进入 `recover_context`。原尝试须终结且没有正文和工具调用；降低输入目标后，只有实际更小且已持久记录的投影可以沿原模型动作自动重试一次。原失败与 usage 归档保留。超时、流中断、未决副作用、取消及预算不足不授权自动重发；自定义图不自动插入恢复节点。

产物页以字节续读。命令日志的完整性根据同一 Run 的原动作回执和精确 artifact ID 核对；原始捕获已截断时，即使下载了整个保存文件，也不能宣称原始输出完整。未知历史来源保留 unknown。
