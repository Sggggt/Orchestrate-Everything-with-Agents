# 重任务协作

用于需要多个独立上下文、阶段依赖和实际验收的工作。常规单轮对话不加载本流程。规划由外部主 Agent 完成，平台不会自动选择最优模型或改派任务。

1. 固定目标、project/session、来源版本、交付项和核验方法。先用确定性目录/索引切分大资料，避免每个实例重新读全量来源。已有会话可以容纳多个互不拥有对方任务的工作流运行。
2. `admission.candidates` 用同项目、required_tools/required_skills、模型及实际执行能力筛选，分页读。对选中的 Agent 用 `admission.describe` 核实声明、授权、可用容量与观测时间。缺配置是缺项，容量占满是等待；先用单实例跑通真实模型响应、实际工具与结果核对，目录或短连通成功不能代替该样本。不要把混合任务延时当作公平排名。
3. 按主入口的选择规则确定函数、turn、实例批次、工作流/map 或一次性依赖。给每项任务传当前输入和必要证据引用，声明 `result_policy`；硬性工具/Skill 需求通过 policy 表达。`admission.check {spec,budget_id?}` 只读检查已有线程的 TaskSpec；不自动创建线程，也不能证明整个图或真实业务可行。图用 `workflows.validate`。
4. 需要多次提交共享总量时，用 `budgets.create` 建会话预算，再把 budget_id 交给任务、批次或工作流入口。普通任务默认也有累计预算；摘要、继续、子任务和修复继承其账本。`progress_tools` 默认不包含任意 MCP；按真实证据工具配置稳定标识，例如 `mcp:source:read_record`，失败、截断和重复内容不算进展。额度耗尽不能靠新实例绕过。用户授权调整额度时，先读 `budgets.get` 的 policy_hash，再用 `budgets.amend` 提交完整策略、稳定键和理由；保留累计消耗和原起始时间，不自动扩额。
5. 查询当前派发动作的 guide，保留稳定键、返回的 task/batch/run ID 及拥有关系。工作流显式 session_id、expected_revision；互不依赖的分支选 collect_independent，需要保留失败成员时 map 选 all_settled。默认策略仍是 fail_fast/all_succeeded。
6. 用有限 `result(timeout<=30)` 或 `operations.inbox` 等待事件，只读相关对象。运行中不得把已接收报告为完成。UNCERTAIN 保留隔离容量，沿原回执核对；派发响应丢失不创建新的业务意图。
7. 部分失败先读取保留结果并修根因；相同配置/输入、相同故障且没有新证据时停止重派。独立任务按条件 continue_result 或安全 retry；已终态工作流先用 `workflows.preview_repair` 查看失效范围，再 repair 并读取 get_repair 核对实际范围。旧终态、原线程、输入版本和成功成员证据保留。短小补证可复用合适的已有线程，不整批重派，不为标题或 Markdown 排版续写。
8. 确定性检查覆盖、结构和产物，再按来源证据审查内容。交付完整结果引用、实际覆盖、预算用量、未知与剩余缺项。工具/模型“成功”不是语义核验，缺可靠同条件基线时不能声称优化幅度或最优性。

只需交接事实时发消息/引用，不另派总结任务。分支合并用 `contexts.merge` 的来源版本、目标预期版本和证据选择；冲突需明确裁决，不能整段覆盖另一条历史。具体字段只查当前 action，避免加载其他流程全文。
