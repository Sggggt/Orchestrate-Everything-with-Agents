# orchestration 模块接口

用 LangGraph 编译并执行默认图、业务图、并行、子图和中断。

源码：[src/oea/orchestration/](../../src/oea/orchestration/)

## 执行与恢复契约

- 声明式图编译成 LangGraph；默认 Agent 图与注册的 Python 图复用同一能力节点和持久 checkpointer。图状态只保存身份、小型状态和结果引用，运行期能力通过 context 注入。
- `map` 使用原生 `Send` 分发，结果按输入顺序汇合并保留逐项错误；空输入返回空结果。`all_settled` 收集全部结果；`all_succeeded` 配合 `fail_fast` 时，首个成员失败会立即终止该图并请求取消所属工作。`collect_independent` 等待独立成员结算。
- 取消请求与实际停止分开记录。外部动作仍在途时保留 `needs_review`，迟到结果归入原动作；实际结算后等待显式确认继续。取消只沿 WorkflowRun 的真实子工作归属传播。
- SQLite 检查点与动作回执分别持久化。恢复沿用原图、Run、预算与动作键，已保存的成功结果不重复执行。人工回复绑定原中断身份，稳定恢复键重放只读取原回执。
- 已归档会话拒绝新的根工作；原键重放返回原 Run，已接受工作继续执行。

## Python 接口

`runs.resume` 与 `workflows.resume` 共用 `ResumeExecutionLimits`，只调整 `max_steps`（1..100000）。省略时沿用最近已接受限额；同键异限额冲突，成功输出、原预算及动作键保留。步数耗尽返回 `EXECUTION_STEP_LIMIT`，单位为 LangGraph super-step。

默认图通过 `recover_context` 处理已确认的窗口拒绝：原 attempt 已终结、无正文或工具调用、未取消且授权与预算有效，只有新投影持久化且实际减量后才重试一次。次数随图状态保存，沿用逻辑动作并归档原 attempt。未知发送、超时、断线、无减量和预算不足不自动重发。自定义图需显式接入该能力。

- `orchestration/graphs/default.py`：`AgentState`
- `orchestration/graphs/default.py`：`AgentExecutionContext`
  - `async prepare(self, state: AgentState)`
  - `async call_model(self, state: AgentState)`
  - `async execute_tools(self, state: AgentState)`
  - `async observe(self, state: AgentState)`
  - `async deliver(self, state: AgentState)`
  - `async apply_reply(self, state: AgentState, value)`
- `orchestration/graphs/default.py`：`AgentGraphContext`
- `orchestration/graphs/default.py`：`prepare(state: AgentState, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/default.py`：`model(state: AgentState, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/default.py`：`tools(state: AgentState, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/default.py`：`observe(state: AgentState, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/default.py`：`deliver(state: AgentState, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/default.py`：`wait(state: AgentState, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/default.py`：`compile_agent_graph(checkpointer)`
- `orchestration/graphs/process.py`：`prepare(state, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/process.py`：`execute(state, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/process.py`：`wait(state, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/process.py`：`deliver(state, runtime: Runtime[AgentGraphContext])`
- `orchestration/graphs/process.py`：`compile_process_graph(checkpointer)`
- `orchestration/graphs/templates.py`：`references(value)`
- `orchestration/graphs/templates.py`：`lookup(scope, path)`
- `orchestration/graphs/templates.py`：`resolve(value, scope, depth=0)`
- `orchestration/graphs/templates.py`：`compare(left, operator, right)`
- `orchestration/graphs/workflow.py`：`merge(previous, update)`
- `orchestration/graphs/workflow.py`：`WorkflowState`
- `orchestration/graphs/workflow.py`：`MapItem`
- `orchestration/graphs/workflow.py`：`WorkflowGraphContext`
- `orchestration/graphs/workflow.py`：`ordinary(node)`
- `orchestration/graphs/workflow.py`：`dispatch(node)`
- `orchestration/graphs/workflow.py`：`member(node)`
- `orchestration/graphs/workflow.py`：`child_wait(node)`
- `orchestration/graphs/workflow.py`：`input_wait(node)`
- `orchestration/graphs/workflow.py`：`names(node)`
- `orchestration/graphs/workflow.py`：`route_map(node, state)`
- `orchestration/graphs/workflow.py`：`done(node)`
- `orchestration/graphs/workflow.py`：`compile_workflow(definition, checkpointer)`
- `orchestration/graphs/workflow_context.py`：`WorkflowContext`
  - `iteration(self, node, state)`
  - `async step(self, node, state, arguments, *, unblocked=False)`
  - `async save(self, node, step, state, reference=None, error=None)`
  - `update(self, node, state, step, *, failed=None, **other)`
  - `recorded_failure(self, node, state, step, *, dispatched=False)`
  - `async outputs(self, run_ids, *, include_results=True)`
  - `async materialize(self, step_id, version=None)`
  - `async scope(self, refs, value, *, item=None, index=None, versions=None)`
  - `async arguments(self, node, state)`
  - `blocked(self, node, state)`
  - `input_refs(self, node, state)`
  - `async skip(self, node, state)`
  - `async execute(self, node, state)`
  - `async dispatch_agent(self, node, state)`
  - `async prepare_map(self, node, state)`
  - `async map_member(self, node, item)`
  - `async map_member_error(self, item, error)`
  - `async dispatch_subgraph(self, node, state)`
  - `async wait_descriptor(self, node, state)`
  - `async dispatch_error(self, node, state, error)`
  - `async prepare_input(self, node, state)`
  - `async cached_dispatch(self, node, state)`
  - `async accept_input(self, node, state, step, value)`
  - `async settle(self, node, state)`
- `orchestration/registry.py`：`AgentGraphs`
  - `register(self, name, factory, *, version)`
  - `available(self, execution)`
  - `resolve(self, execution)`
  - `async list(self)`
- `orchestration/service.py`：`Workflows`
  - `register_function(self, function, *, name=None)`
  - `notify(self, run_id, *, settled=False)`
  - `signal(self, run_id)`
  - `project_id(run)`
  - `async node_types(self)`
  - `async schema(self)`
  - `async create(self, project_id: str, name: str, graph: WorkflowDraft, *, workflow_id: str | None=None, description: str='')`
  - `async get(self, workflow_id: str, project_id: str)`
  - `async list(self, project_id: str, *, cursor: int=0, limit: int=25, include_archived: bool=False, deleted_only: bool=False)`
  - `async update(self, workflow_id: str, project_id: str, expected_revision: int, *, graph: WorkflowDraft | None=None, name: str | None=None, description: str | None=None)`
  - `async validate(self, project_id: str, graph: dict)`
  - `async activity(self, project_id: str, *, cursor: int=0, limit: int=100)`
  - `async set_archived(self, workflow_id: str, project_id: str, archived: bool, expected_revision: int)`
  - `async remove(self, workflow_id: str, project_id: str, expected_revision: int, *, confirmed: bool=False)`
  - `async restore(self, workflow_id: str, project_id: str, expected_revision: int)`
  - `async run(self, workflow_id: str, inputs: dict, idempotency_key: str, *, project_id: str, session_id: str | None=None, expected_revision: int | None=None, budget_id: str | None=None, parent_workflow_run_id: str | None=None)`
  - `async get_run(self, workflow_run_id: str, *, project_id: str)`
  - `async list_runs(self, workflow_id: str, *, project_id: str, cursor: int=0, limit: int=25)`
  - `async inspect(self, workflow_run_id: str, *, project_id: str)`
  - `async node_result(self, workflow_run_id: str, node_id: str, *, project_id: str, step_id: str | None=None, version: str | None=None, metadata_only: bool=False)`
  - `async members(self, workflow_run_id: str, node_id: str, *, project_id: str, step_id: str | None=None, version: str | None=None, cursor: int=0, limit: int=25)`
  - `async result(self, workflow_run_id: str, *, project_id: str, timeout: float=0)`
  - `async compute(self, context, node, step, arguments)`
  - `inflight(self, run_id)`
  - `config(run_id, definition)`
  - `launch(self, run_id, *, restart=False)`
  - `async finish(self, run_id, state, *, error=None, wait=None, result_ref=None)`
  - `async resume(self, workflow_run_id: str, idempotency_key: str, *, project_id: str, input: str | dict | None=None, interrupt_id: str | None=None, node_id: str | None=None, execution_limits: ResumeExecutionLimits | None=None)`
  - `async cancel(self, workflow_run_id: str, *, project_id: str)`
  - `async open(self)`
  - `async aclose(self)`
- `orchestration/types.py`：`Position`
- `orchestration/types.py`：`Viewport`
- `orchestration/types.py`：`StartConfig`
  - `valid_input_schema(cls, value)`
- `orchestration/types.py`：`AgentNodeConfig`
- `orchestration/types.py`：`MapConfig`
- `orchestration/types.py`：`ConditionConfig`
- `orchestration/types.py`：`TemplateConfig`
- `orchestration/types.py`：`FunctionConfig`
- `orchestration/types.py`：`SubgraphConfig`
- `orchestration/types.py`：`WaitConfig`
- `orchestration/types.py`：`EndConfig`
- `orchestration/types.py`：`Node`
- `orchestration/types.py`：`StartNode`
- `orchestration/types.py`：`AgentNode`
- `orchestration/types.py`：`MapNode`
- `orchestration/types.py`：`ConditionNode`
- `orchestration/types.py`：`TemplateNode`
- `orchestration/types.py`：`FunctionNode`
- `orchestration/types.py`：`SubgraphNode`
- `orchestration/types.py`：`WaitNode`
- `orchestration/types.py`：`EndNode`
- `orchestration/types.py`：`Edge`
- `orchestration/types.py`：`WorkflowDraft`
- `orchestration/types.py`：`WorkflowGraph`
  - `references(self)`
- `orchestration/types.py`：`WorkflowBuilder`
  - `node(self, identity, kind, *, title='', x=0, y=0, join='all', **config)`
  - `connect(self, source, target, *, branch='default')`
  - `build(self, *, failure_policy='fail_fast', budget=None, max_steps=512)`

## 依赖与边界

依赖模块：catalog、contracts、runtime、stdio、storage、tools。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

默认 Agent 图的窗口拒绝路径为 `model → recover_context → model`，只在确认拒绝后进入，正常路径不增加节点或模型调用。重试标记位于原图状态，恢复证据位于原事件记录。工作流与 Agent 的 `execution_limits.max_steps` 都表示每次 LangGraph 调用的 super-step 上限。

Agent 节点以 `title` 和节点 ID 生成 Run 的展示元信息。Map 的可选 `item_label` 使用原模板作用域解析为最多 120 字符的文本，缺省使用条目序号；这些名称不需要模型调用，不改变原 Run ID、版本或验收结论。
