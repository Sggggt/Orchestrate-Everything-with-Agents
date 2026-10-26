<p align="center">
  <img src="src/oea/web/static/mark.svg" width="96" height="96" alt="Orchestrate Everything with Agents logo">
</p>

<h1 align="center">Orchestrate Everything with Agents</h1>

<p align="center">你的 Agent，你的工具，由你编排。</p>
<p align="center"><a href="README.md">English</a> · 简体中文</p>

Orchestrate Everything with Agents 是一个轻量化、面向个人的多智能体开发和协作平台。你可以在我们的框架内开发和编排任何东西：自己的 Agent、工具、代码、模型与工作流，都能汇聚到本机的同一个工作空间。从一段对话开始，把重复工作连接成图，让各个 Agent 保持独立上下文，朝着共同目标协作。当然，如果你乐意的话，你可以让你的ChatGPT操控这一切。

## 可以用它做什么

- **搭建个人 Agent 工作空间。** 按项目管理 Agent、会话、指令和技能。
- **开发自己的执行逻辑。** 配置模型 Agent，编写 v2 Python 进程 Agent，或注册由 LangGraph 驱动的代码图。
- **并行启动独立实例。** 复用 Agent 配置，按输入批量创建独立上下文；保留有序结果，并沿实际工作归属取消。
- **编排可复用的工作流。** 在画布或 Python SDK 中连接开始、Agent、并行 map、条件、模板、函数、子图、中断和结束节点；每个工作流只属于一个项目。
- **工具和工作产物。** 注册 MCP 服务，按需授权工具，交换完整协作消息；会话文件保留来源记录，并可通过引用下载。
- **分配实际工作能力。** 通过统一[工作底座](docs/workbench.md)授权文件修改、图片、产物和可信本机命令，按任务复用外部语言环境，无需容器。
- **在本机持久运行。** 任务回执、检查点、运行历史与备份保存在源码目录之外，明确处理取消和结果不确定的情况。
- **通过 MCP 操作平台。** 用两个紧凑工具和按需加载的 Codex 技能接入兼容客户端，浏览器负责配置与观察。

前端直接使用本地静态资源，无需构建。任务与依赖、协作消息、工作产物集中在会话总览顶部标签页，新活动以红点提醒。工作流画布支持校验、版本检查、导入导出、删除与回收站恢复。

## 本机启动

需要 **Python 3.12 或更新版本**。在仓库外创建 Python 环境，然后从源码仓库安装 Web 和 MCP 组件：

```sh
python -m pip install -c constraints.txt '.[web,mcp]'
oea serve
```

打开 **http://127.0.0.1:8890**，本机页面会自动建立经过认证的浏览器会话。Windows 用户先运行 **Install-OEA.cmd**，再打开 **Start-Workspace.cmd**；安装器自动准备仓库外的托管环境。各系统的完整环境命令和数据目录说明见[快速开始](docs/getting-started.md)。

创建项目、添加 Agent，再开启会话。需要模型的 Agent 请在 **Agent → 配置 → 模型 API** 中保存 API 地址、模型和密钥，并测试已保存配置。密钥保存在本机，不进入源码仓库。

启动不注册演示 Agent，也不发起真实模型调用。命令和进程 Agent 需显式加 `--allow-commands`，并具有已保存的工具授权。

## 用 Python 编排

```python
from oea import PlatformClient, WorkflowBuilder

graph = (
    WorkflowBuilder()
    .node("start", "start")
    .node("format", "template", value={"text": "你好，{{inputs.name}}。"})
    .node("end", "end", outputs={"text": "{{nodes.format.text}}"})
    .connect("start", "format")
    .connect("format", "end")
    .build()
)

async def run(data_dir, project_id):
    async with PlatformClient.from_data_dir(data_dir) as client:
        workflow = await client.call("workflows.create", {
            "project_id": project_id, "name": "你好工作流",
            "graph": graph.model_dump(mode="json")})
        receipt = await client.call("workflows.run", {
            "workflow_id": workflow["id"], "inputs": {"name": "世界"},
            "idempotency_key": "hello-run-001", "project_id": project_id})
        return await client.call("workflows.result", {
            "workflow_run_id": receipt["id"], "project_id": project_id, "timeout": 10})
```

传入已有项目 ID 和宿主使用的外部数据目录。恢复同一次请求时沿用原幂等键；真正开始下一次运行时使用新键。可直接执行的[工作流示例](examples/workflow.py)和 [Agent 示例](examples/model_agent.py)提供后续参考。

## 让客户端操作这一切

```sh
oea mcp
```

桥接进程连接已有宿主，提供 `platform_guide` 与 `platform_call`。从前端复制生成的配置，再安装仓库提供的 [Codex 技能](integrations/codex/orchestrate-everything-with-agents/SKILL.md)。安装、按需发现接口和权限说明见 [MCP 与技能指南](docs/mcp.md)。连接 ChatGPT 时，所用客户端环境需能够访问选定的 MCP 传输。

## 文档

[快速开始](docs/getting-started.md) · [SDK](docs/sdk.md) · [工作流](docs/workflows.md) · [MCP 与技能](docs/mcp.md) · [API](docs/api.md) · [架构](docs/architecture.md) · [品牌](docs/brand.md)

运行数据必须位于 Git 仓库和同步文件夹之外的本机磁盘。连接已有宿主使用 `PlatformClient`，同一数据目录只由一个 Host 拥有。Agent 是需要信任的本机程序，受管子进程不等于操作系统沙箱。LangGraph 统一负责图、循环、并行汇合和持久中断。

## 参与贡献与许可证

环境和验证方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，敏感问题报告方式见 [SECURITY.md](SECURITY.md)。仓库保留通用自动化契约测试及源码 fixture，不包含已填充的工作空间或生成的测试结果。

采用 [MIT 许可证](LICENSE)。版本 2 统一使用 `oea` 包、命令与协议。旧状态先离线转换到独立新目录，详见[迁移](docs/migration/README.md)。
