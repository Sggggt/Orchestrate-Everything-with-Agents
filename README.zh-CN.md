<p align="center">
  <img src="src/ma_runtime/web/static/mark.svg" width="96" height="96" alt="Orchestrate Everything with Agents logo">
</p>

<h1 align="center">Orchestrate Everything with Agents</h1>

<p align="center">你的 Agent，你的工具，由你编排。</p>
<p align="center"><a href="README.md">English</a> · 简体中文</p>

Orchestrate Everything with Agents 是一个轻量化、面向个人的多智能体开发和协作平台。你可以在我们的框架内开发和编排任何东西：自己的 Agent、工具、代码、模型与工作流，都能汇聚到本机的同一个工作空间。从一段对话开始，把重复工作连接成图，让各个 Agent 保持独立上下文，朝着共同目标协作。当然，如果你乐意的话，你可以让你的ChatGPT操控这一切。

## 可以用它做什么

- **搭建个人 Agent 工作空间。** 按项目管理 Agent、会话、指令和技能。
- **开发自己的执行逻辑。** 配置 LLM Agent，编写 Python 进程 Agent，或封装 LangGraph 等框架。
- **并行启动独立实例。** 复用 Agent 配置，按输入批量创建独立上下文；支持 Agent 池路由、批次限流、有序结果与取消。
- **编排可复用的工作流。** 在画布或 Python SDK 中连接开始、Agent、并行实例 map、条件、模板和结束节点；每个工作流只属于一个项目。
- **工具和工作产物。** 注册 MCP 服务，按需授权工具，交换完整协作消息；会话文件保留来源记录，并可通过引用下载。
- **分配实际工作能力。** 通过统一[工作底座](docs/workbench.md)授权文件修改、图片、产物和可信本机命令，按任务复用外部语言环境，无需容器。
- **在本机持久运行。** 任务回执、检查点、运行历史与备份保存在源码目录之外，明确处理取消和结果不确定的情况。
- **通过 MCP 操作平台。** 用两个紧凑工具和按需加载的 Codex 技能接入兼容客户端，浏览器负责配置与观察。

前端直接使用本地静态资源，无需构建。任务与依赖、协作消息、工作产物集中在会话总览顶部标签页，新活动以红点提醒。工作流画布支持校验、版本检查、导入导出、删除与回收站恢复。

## 本机启动

需要 **Python 3.12 或更新版本**。在仓库外创建 Python 环境，然后从源码仓库安装 Web 和 MCP 组件：

```sh
python -m pip install -c constraints.txt '.[web,mcp]'
oea serve --port 8890
```

打开 **http://127.0.0.1:8890**，本机页面会自动建立经过认证的浏览器会话。Windows 用户可以直接运行 **Start-Workspace.cmd**，完成环境准备与启动。各系统的完整环境命令和数据目录说明见[快速开始](docs/getting-started.md)。

创建项目、添加 Agent，再开启会话。需要模型的 Agent 请在 **Agent → 配置 → 模型 API** 中保存 API 地址、模型和密钥，并测试已保存配置。密钥保存在本机，不进入源码仓库。

无需模型的体验可使用 `oea serve --mock --port 8890 --data-dir EXTERNAL_DIRECTORY`，并指定单独的空目录。普通启动不会填充演示会话或任务。

## 用 Python 编排

```python
from oea import PlatformClient, WorkflowBuilder

graph = (
    WorkflowBuilder()
    .node("start", "start")
    .node("format", "template", template="你好，{{inputs.name}}。")
    .node("end", "end", outputs={"text": "{{nodes.format.text}}"})
    .connect("start", "format")
    .connect("format", "end")
    .build()
)

async def run(data_dir, project_id):
    async with PlatformClient.from_data_dir(data_dir) as client:
        workflow = await client.workflows.create(
            project_id, "你好工作流", "hello-definition-v1", graph=graph)
        receipt = await client.workflows.run(
            workflow["id"], {"name": "世界"}, "hello-run-001",
            expected_revision=workflow["revision"])
        return await client.workflows.result(receipt["id"], timeout=10)
```

传入已有项目 ID 和宿主使用的外部数据目录。恢复同一次请求时沿用原幂等键；真正开始下一次运行时使用新键。可直接执行的[工作流示例](examples/workflow.py)和 [Agent 示例](examples/model_agent.py)提供后续参考。

## 让客户端操作这一切

```sh
oea mcp --connect --compact --data-dir EXTERNAL_DIRECTORY
```

桥接进程连接已有宿主，提供 `platform_guide` 与 `platform_call`。从前端复制生成的配置，再安装仓库提供的 [Codex 技能](integrations/codex/orchestrate-everything-with-agents/SKILL.md)。安装、按需发现接口和权限说明见 [MCP 与技能指南](docs/mcp.md)。连接 ChatGPT 时，所用客户端环境需能够访问选定的 MCP 传输。

## 文档

[快速开始](docs/getting-started.md) · [SDK](docs/sdk.md) · [工作流](docs/workflows.md) · [MCP 与技能](docs/mcp.md) · [API](docs/api.md) · [架构](docs/architecture.md) · [品牌](docs/brand.md)

运行数据必须位于 Git 仓库和同步文件夹之外的本机磁盘。连接已有宿主使用 `PlatformClient`，不要对同一数据目录再打开第二个 Runtime。Agent 是需要信任的本机程序，受管子进程不等于操作系统沙箱。画布支持有向无环图；循环和自定义代码由 Agent 内部实现。

## 参与贡献与许可证

环境和验证方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，敏感问题报告方式见 [SECURITY.md](SECURITY.md)。仓库保留通用自动化契约测试及源码 fixture，不包含已填充的工作空间或生成的测试结果。

采用 [MIT 许可证](LICENSE)。现有 `ma_runtime` 导入和 `multi-agent` 命令继续兼容，新接入统一使用 `oea`。
