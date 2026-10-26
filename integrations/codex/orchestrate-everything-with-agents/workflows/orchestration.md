# 业务图

1. 固定项目、输入、预算和交付条件，确定普通函数与独立 Agent 的边界。
2. 用 `workflows.schema`／`workflows.node_types` 按需查看定义。图包含 start、agent、map、condition、template、function、subgraph、wait、end；代码图通过 executions.graphs 观察。
3. `workflows.create` 保存新定义；修改使用 `workflows.update` 和 expected_revision。agent/map 只引用同项目或明确全局 Agent。普通函数在宿主注册。
4. `workflows.run` 使用稳定意图键和 project_id。map 使用原生 Send；条件定义 true/false 两路；并行汇合 join=all，替代路径或循环入口使用 join=any。
5. 读取 `get_run`／`inspect` 与逐项 `node_result`。完整交付用 `result`，按 result_ref 读取长结果。保留成功分支、失败证据和原产物。
6. 人工等待用原 interrupt_id 和 `workflows.resume`，输入应用一次。普通依赖完成由持久回执唤醒。失败修复关联原 WorkflowRun，可选 node_id 限定失败分支；不从 start 盲重建图。
7. 取消原 WorkflowRun，核对实际所属子工作与停止事实。未知外部结果先核对，不通过新图换键重放。

原生检查点重放仍可能重新进入节点，OEA 稳定回执复用已提交工作。图结束、交付验证、实际内容正确分别核验。
