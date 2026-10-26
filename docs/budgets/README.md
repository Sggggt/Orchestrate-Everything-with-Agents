# budgets 模块接口

工作根预算、在途预留和真实用量；不承担并发容量准入。

源码：[src/oea/budgets/](../../src/oea/budgets/)

## Python 接口

- `budgets/service.py`：`Budgets`
  - `async create(self, project_id: str, policy: Budget, idempotency_key: str, *, session_id: str | None=None)`
  - `require_scope(db, budget_id, *, project_id, session_id)`
  - `model(self, db, run, identity, purpose, input_tokens, output_tokens)`
  - `settle(db, identity, usage)`
  - `tool(self, db, run, identity, name)`
  - `progress(db, run_id, source, value)`
  - `async get(self, budget_id: str)`

## 依赖与边界

依赖模块：catalog、contracts、storage。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 用量口径

`usage` 是预算实际采用的核算总量，可能包含报告值、估算和未决预留。`provider_reported` 替代含义不清的 actual 字段，逐项返回供应商报告和覆盖次数；缺失报告为 null，报告值不是最终账单。缓存输入是输入子集，单独展示，不再次相加。缓存报告超出输入时，报告视图返回 null 并计入 invalid_cache_reports，原始报告仍保留在动作回执中。

`estimated`、`reserved`、`needs_review` 分别展示已结算估算、在途预留、待核对部分。只有部分 usage 字段时，已报告字段仍可见，预算继续按现有估算规则核算该调用；这些展示分项不能机械相加。

`attempts` 分别统计调用尝试、发送前拒绝、收到响应、生成完成、失败和各阶段的已知覆盖次数。旧回执缺少阶段事实时计入 historical_unknown，不从最终状态推断发送或计费。发送前拒绝的零值属于本地预算核算，不能伪装成供应商报告零用量。

新增事实复用 actions、usage、model_failures 和事件的 JSON 内容，不改变 SQLite Schema；旧记录保留未知字段，不补写历史事实。
