# 配置 Agent

1. 读取 `projects.get`／`projects.list` 和当前 `agents.get`；固定项目和修订。新项目使用 `projects.create`，已有对象不重复创建。
2. 缺少字段定义时查询 `agents.schema`。保存 `AgentDefinition` 六要素，保留当前完整定义后只改目标字段，`agents.save` 携带 `expected_revision`。
3. 模型端点和模型名写入 model；凭据经本地设置或 `agents.set_credential` 保存，只读回引用和 configured。manager 使用自己的模型、秘密、指令、通信和 Context。
4. local、functions、mcp 分别绑定实际工具。Python 函数须在宿主进程注册；共享 Skill 按 id 绑定，名称和描述随正文版本固定，required 才预加载；直接目录绑定可显式提供用途元数据。Workspace 原件只读，明确工作目录可写；命令还需要宿主 grant。
5. 用 `agents.check` 核对 main／manager 各自的配置与密钥来源、当前授权、目录预览和程序查找结果。它不创建工作目录、不发起模型请求，不能作为真实模型或业务验收。开始实际执行前明确材料、出站目的地、预算和验收条件。

读取修订冲突后的最新定义再合并当前修改。缺少秘密时保留配置并提示通过本地设置填写，不在对话中索取密钥。
