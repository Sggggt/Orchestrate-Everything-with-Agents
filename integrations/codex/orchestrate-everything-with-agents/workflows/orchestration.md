# 项目工作流与画布编排

用于创建、编辑、校验、运行可保存的项目工作流。先固定真实 project_id；工作流创建后不能改项目。一次性会话任务依赖继续用 [tasks.md](tasks.md) 的 tasks.workflow。

按当前需要查询 `platform_guide(area="workflows", action="...")`，不要展开全部协议。节点配置不清楚时调用 `workflows.node_types`；图类型按需查询 `development.workflow_graph`。源码示例和画布行为见仓库 `docs/workflows.md`、`examples/workflow.py`。

## 设计与保存

- `list {project_id, limit:25}` / `get {workflow_id}` 固定定义与 revision；分页按 cursor 继续。
- `create {project_id, name, graph, description, idempotency_key}` 创建独立定义；省略 graph 得到开始/结束草稿。project_id 必填，不能借其他项目的 Agent。
- graph 包含 nodes、edges、viewport。node 包含 id/type/title/position/config；edge 包含 id/source/target/source_handle。支持 start、agent、map、condition、template、end；图为无环图。Agent 节点使用项目内已注册的受管执行端。
- 变量使用 `{{inputs.name}}`、`{{nodes.node_id.field}}` 或 `{"$ref":"nodes.node_id.field","default":null}`；完整引用保留类型，只能引用前置节点。条件出口 source_handle 为 true/false，其他节点为 default。合流等待全部路径结束，至少一条有效路径时继续；未选中的路径跳过。
- `validate {project_id, graph}` 返回错误、警告及顺序。草稿允许不完整；运行前修复错误。模型未配置的警告提示去前端完成配置，不反复创建任务。
- `update {workflow_id, expected_revision, graph, name, description}` 保存编辑。REVISION_CONFLICT 时读取最新版本并合并，不能用新 revision 盲目覆盖。运行快照不受后续编辑影响。
- `archive {workflow_id, archived:true/false}` 归档或恢复；历史保留，已有运行继续。
- `delete {workflow_id, confirmed:true}` 在用户已授权删除此工作流后移入回收站；未完成或待核对的运行会阻止删除。画布、项目归属和所有运行历史保留，不能把归档当作删除。
- `list {project_id, deleted_only:true, limit:25}` 查看该项目工作流回收站；`restore {workflow_id}` 恢复到删除前的活动/归档状态。父项目已删除或归档时先恢复/打开项目；恢复不重新执行任务。

前端“工作流画布”可拖动节点、端口连线、编辑参数、平移缩放、自动布局、撤销重做和导入导出。SDK/MCP 编辑的 graph 会显示在同一画布；position 与 viewport 是展示信息。

## 列表并行实例

map 节点配置 agent_ids、items、payload、max_concurrency、routing。items 引用输入或上游列表，最多 128 项；payload 可引用当前 item/index，也可引用 inputs/nodes。agent_ids 是同项目受管 Agent 池，routing 为 round_robin 或 least_loaded。节点复用 instances 批次与普通 Task 调度，默认 result_mode=all_succeeded，按输入顺序在 outputs 字段汇聚全部成功结果，count 给出项数。显式 all_settled 等待真实终态后在 results 中保留每个成员的状态、完整结果/错误与 ordinal；counts 给出计数，UNCERTAIN 仍需核对。空列表直接成功。不支持首胜/quorum 或额外并发池。

get_run 的 map 节点返回 batch_id 和实例 task_id/ordinal/state。恢复或核对某个成员时，resume_node/reconcile_node 额外提供 instance_index；取消仍对整个工作流生效。若成员结果 UNCERTAIN，先核对具体 Task，不重新运行 map。一次性动态委派参见[多实例编排](instances.md)。

## 运行与核对

`run {workflow_id,inputs:{...},idempotency_key,expected_revision,session_id?,budget_id?}` 使用固定版本。显式 session_id 复用同项目 OPEN 会话，省略才创建会话；多个运行各自拥有任务，取消不影响其他运行和普通任务。显式 budget_id 可让多次运行共用会话预算。inputs 要符合开始节点的 object JSON Schema。保留 workflow_id/run_id/session_id/revision 和稳定幂等键。同键同参数重试读取原运行；客户端超时或断连先用 runs/get_run 核对，不换键重跑。

`runs {workflow_id,limit:25,order:"active"}` 可优先看活动运行，默认按创建记录分页；`get_run {run_id}` 查看节点状态、task_id、摘要、完整输出与错误。`result {run_id, timeout:0}` 默认非阻塞，显式等待最多 30 秒。WAIT_TIMEOUT 只结束等待。Agent 节点共用平台执行预算及该 Agent 的模型和工具授权，不增加未受管执行池。

WAITING 时查关联 `tasks.get`：若 wait.kind=input，用真实输入和返回的 wait.wait_token 调用 `resume_node {run_id,node_id,input:{wait_token,...},idempotency_key}`。等待消息/其他任务由原规则唤醒。queue_reason=MODEL_CONFIGURATION 时提醒去“Agent 集成 → 配置 → 模型 API”保存并测试，保留原任务。

UNCERTAIN 会阻止新下游执行。查 `reconcile_node` 的 Reconciliation 协议，仅在真实停止证据和外部结果已核实后处置，不能虚构确认，也不能自动重试。`cancel {run_id}` 只是取消申请；确认所有任务结束后才报告 CANCELLED，未确定时继续保留 UNCERTAIN。开始节点 failure_policy 默认 fail_fast，节点失败会请求停止该运行其他任务；显式 collect_independent 只阻断失败节点的依赖路径，独立路径继续并保留结果。最终运行仍按实际失败结算。

工作流未完成时，项目归档、删除、换文件夹和运行会话关闭/删除均被拒绝。运行会话不能移入其他项目。工作流通过 Agent 继承既有工具边界，不能借编排绕过领域技能或工具授权。

## 局部修复

真实终态且未知外部效果已核对后，调用 workflows.repair(source_run_id,idempotency_key)，必要时提供 inputs、expected_revision、rerun_nodes。省略版本使用源图，指定版本使用当前已保存图；输入、节点、依赖变化会使受影响结果失效。新运行和新线程保留来源，成功节点/成员按版本与授权检查后复用。get_repair 查看复用范围与来源，不重绑旧线程、不改原终态；修复累计原预算。
