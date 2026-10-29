# 配置 Agent

1. 读取 `projects.get`／`projects.list` 和当前 `agents.get`；固定项目和修订。新项目使用 `projects.create`，已有对象不重复创建。
2. 缺少字段定义时查询 `agents.schema`。保存 `AgentDefinition` 六要素，保留当前完整定义后只改目标字段，`agents.save` 携带 `expected_revision`。
3. 模型端点和模型名写入 model；凭据经本地设置或 `agents.set_credential` 保存，只读回引用和 configured。manager 使用自己的模型、秘密、指令、通信和 Context。
4. local、functions、mcp 分别绑定实际工具。Python 函数须在宿主进程注册；共享 Skill 按 id 绑定，名称和描述随正文版本固定，required 才预加载；直接目录绑定可显式提供用途元数据。Workspace 原件只读，明确工作目录可写；命令还需要宿主 grant。
5. 用 `agents.check` 核对 main／manager 各自的配置与密钥来源、当前授权、目录预览和程序查找结果。它不创建工作目录、不发起模型请求，不能作为真实模型或业务验收。开始实际执行前明确材料、出站目的地、预算和验收条件。
6. 用户需要模型连接测试时，使用 `agents.probe(agent_id, session_id, idempotency_key, role)`，通过返回的原 Run 查询结果及用量；不要在普通业务 Run 中仅写“只回复 READY”。探测不加载业务能力，最多一次模型调用与 128 输出 token。main／manager 独立，manager 仍要求协作模式；超时保留原键并读回执，不自动重派。
7. 用户要求删除登记时，按对象查询单个 `agents.remove`／`mcp.remove`／`skills.remove` 协议，携带当前项目、对象 ID 与 expected_revision。删除从活动目录隐藏并撤权，保留历史和物理源文件；源缺失不阻止删除。Agent 存在未结束或待核对工作时先报告并处理原工作，不自动取消。关联配置不静默改写；响应未知先核对原对象与修订，不改键重派。
8. 用户明确确认项目永久删除时，使用 `projects.delete` 的精确名称与稳定键。该动作撤销项目工作并注销全部所属平台记录、未决账目及图检查点，不要求先把遗留状态改成成功或已结算。原件、备份与其它项目保留，已发出的外部效果不自动回滚；清理中断沿原键继续，不重新执行业务动作。

业务工具参数默认 `tools.loading=deferred`，模型用 tool_guide 按需选择后读取原生完整 Schema；少量固定工具可显式 eager。Skill 默认只提供目录，按当前任务读取正文及参考；required 仅保留每次都必需的指令，不因优化静默撤销既有约束。

读取修订冲突后的最新定义再合并当前修改。缺少秘密时保留配置并提示通过本地设置填写，不在对话中索取密钥。
