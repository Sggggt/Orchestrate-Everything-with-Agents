# context 模块接口

原文、上下文引用、历史、阶段记忆及有界模型投影。

源码：[src/oea/context/](../../src/oea/context/)

## 投影与历史边界

- 原文、阶段记忆、活跃模型投影分别保存。大工具输出先持久化，再以有界片段和来源引用进入投影；已有动作回执可直接作为原文来源。
- 工具调用和返回成组保留。记忆更新只处理新增历史，并保留原文的读取入口；摘要失败不覆盖既有记忆，已成功的阶段可继续用于恢复。
- 最新阶段记忆按实例和插入顺序选取；活跃投影从该记忆之后最旧的有界历史页开始读取。超出行窗口时自动通过摘要回调推进阶段记忆，无需先达到 token 压力阈值；每次成功均持久化连续历史位置及来源引用，不扫描全历史。
- 跨页的 assistant/tool 组保持完整，只有完成配对后才能进入摘要或活跃投影。较大的摘要按完整组拆为符合有效输入预算的连续增量；单组连同已有记忆仍超出预算时返回明确的模型上下文错误，原文保留。
- `input_tokens` 为显式的 UTF-8 启发式估算，包含当前消息与工具 Schema；有效输入窗口还扣除模型输出和安全余量。该估算与模型供应商回执、实际账单分开记录。
- 原文读取验证项目、实例或显式共享授权；来源对应的工具或 Skill 在读取时仍须满足当前权限。

## Python 接口

- `context/store.py`：`source_permission(origin)`
- `context/store.py`：`estimated_tokens(value)`
- `context/store.py`：`message_groups(messages)`
- `context/store.py`：`prune_text(text, token_budget, notice)`
- `context/store.py`：`ContextStore`
  - `check_access(db, instance, source)`
  - `async authorize_reference(self, run, instance, reference, authorize)`
  - `async source(self, run, instance, text, origin, *, action_key=None, on_insert=None)`
  - `async read(self, run, instance, reference, *, offset=0, limit=8192, authorize=None)`
  - `async revoke(self, reference: str, instance_id: str, *, project_id: str)`
  - `async append(self, run, instance, message, key)`
  - `async message(self, instance_id, key)`
  - `async input(self, run, instance)`
  - `async inbox(self, run, instance)`
  - `async reply(self, run, instance, request_id)`
  - `async tool_result(self, run, instance, policy, call, result, *, action_key=None, on_insert=None)`
  - `async history(self, instance_id, *, after=0, limit=20)`
  - `async project(self, run, instance, definition, instructions, tools, *, summarize, output_tokens=None, summary_reserve=None, prepare_tools=None, input_cap=None)`

## 依赖与边界

依赖模块：contracts、storage。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 有界内容选择

投影记录版本、模型和工具集合指纹、历史位置、记忆引用及估算方法。供应商实际输入和估算偏差绑定原请求，缓存命中单独计量。相同契约的重复工具详情仅在投影引用化，原始历史和错误证据保留。压缩按选择指纹复用原摘要回执；只有有效且实际减量的摘要才推进记忆位置，无新增内容不重复摘要。工作输入目标、模型窗口和根累计成本分别核验，估算校准不放宽硬边界。

窗口拒绝恢复沿用本模块的确定性选择与摘要能力，通过更低的 `input_cap` 准备候选；失败保留原记忆与原模型失败。`context.recovered` 记录原 action/attempt、前后输入哈希、估算与投影。模型执行边界验证该证据，不能将它当作用户对未知结果重发的批准。

`context/selection.py` 是纯函数。正常输入走快速路径；压力触发时，在当前有界历史页上为每个完整消息组选择 full 或 reference。引用视图仅卸载可识别的正文，状态、错误、退出码、来源和未决动作保留；assistant 的调用 ID、名称、参数与对应结果不拆分。

先保留全部组的可行视图，确实放不下最小视图时才允许连续前缀摘要。最新完整组及未决动作组禁止进入摘要前缀，recent_turns 只影响固定的近期效用。后缀不能跳过消息；摘要按自身本次输出预留重新计算输入空间，并通过原回调分段提交，原记忆及来源链沿用现有恢复语义。

DP 使用 2048 档容量，必要时细化至 4096。成本向上、容量向下量化，空间使用滚动分数和紧凑回溯记录；摘要前缀阶段只需一个无保留状态。量化无解时核对未量化最小视图并确定性回退，不因量化误拒绝而发起摘要。最优性限于当前候选、量化成本和固定效用。最终投影复核估算预算，实际请求另核对编码字节。
