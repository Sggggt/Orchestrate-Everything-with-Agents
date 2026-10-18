# 分配工作能力

用于给 Agent 分配文件、命令、图片和交付能力。先固定目标项目；不修改无关宿主或模型配置。

1. 用 environments.list 选择项目已有环境，get 读取版本与程序目录。缺少配置时只发现 configure 参数，由宿主授权范围确定程序路径和只读资源。
2. prepare(environment_id, idempotency_key) 返回受管 task_id。用 tasks.get/result 查看准备结果；重复查询沿用回执，不能重新派发。check 同样返回任务回执。
3. 区分 base_ready、task_ready、execution_ready。文件能力不依赖某种语言；缺少程序只影响对应任务。local runner 管理进程树，不提供文件和网络系统隔离。
4. 复用 agents.configure/patch 的 workbench 字段，只选择 environment_id、permission_profile、tools。默认无工作能力；read-only/workspace-write 只规定文件工具权限。
5. 本机命令须选择 trusted-local，且宿主启动时已显式允许。管理 MCP、Skill 或指导消息不能开启该宿主授权。严格隔离缺项时拒绝命令，不降级。
6. 用 agents.capabilities/workbench.inspect 核对分配。派发最小任务，查看实际结果与产物；注册成功不代表执行成功。

## 执行约定

- 文件工具使用 /workspace、/project、/resources 映射路径。任务目录独立；项目默认只读。覆盖文件先读取得 revision，edit_file 要求唯一匹配。
- exec_command 的 argv[0] 选择配置的程序别名。脚本使用相对路径和当前工作目录；不要假设安装了任意语言，也不要在任务中改写共享环境。
- trusted-local 程序使用宿主用户权限。最小环境变量、cwd 和文件工具校验不等于系统沙箱；不向程序提供模型密钥或管理 token。
- read_image 返回会话产物引用，模型请求边界转换图片块。进程/图的模型消息使用 type=oea_image 和 artifact_id；不要把 Base64 存入图状态、日志或工具正文。
- publish_artifact 仅交付获准目录中的文件。文档任务按需绑定 PDF/Documents Skill，不向所有 Agent 注入文档流程。
- 任务策略只收窄现有授权，required 要求成功回执或实际加载证据。

## 结果与故障

workbench.calls 按任务分页返回调用状态；选定 tool_call_id 后读取单个完整回执。大结果按 result_ref 和 pointer 读取，常用 limit=2000。

超时或断连先查任务和回执。UNCERTAIN 保留占用，不换调用键重放。workbench.reconcile 查询已有探测，或在确认进程停止并核对副作用后提交有证据的 resolution；核对本身不重跑命令。

故障通过已有 report_issue/operations 请求指导。仅在明确准备配方与宿主授权内安装依赖；不安装容器引擎或重启现有宿主。

开发 LangGraph 执行端时转到 [LangGraph 参考](../references/langgraph.md)，当前任务只读这一份参考。
