# 运行与核验

1. 首次在线用 `host.status` 确认版本与宿主。分页读取项目、会话、实例和 runs.list，保留实际 ID。
2. 独立上下文用 instances.create；追加到既有实例使用 runs.submit，携带原意图幂等键。一个实例只有一个写入者，独立实例可并行。
3. `runs.get` 看状态，`runs.inspect` 看动作、预算和产物。完整结果用 runs.result；大输出通过 results.read，模型历史用 runs.attempts 和固定 attempt_id 的 runs.action。
4. waiting 输入用 runs.resume 和原 Run；失败先看原错误、检查点与动作。修复配置后显式继续，默认在该边界刷新模型与能力。不要把未决外部动作伪装成普通失败。
5. `runs.cancel` 表示请求取消；核对 cancellation、stopped_at 和原动作，区分已停止和 needs_review。晚到结果保留在原 action，不自动重派。
6. events.list 按持久游标补读，SSE 只提供实时观察；丢帧后读取事实，不重新执行操作。
7. 独立核对产物内容、来源和实际业务效果。报告实际完成、真实调用量和未验证范围，不把配置成功或状态绿色当作业务正确。
