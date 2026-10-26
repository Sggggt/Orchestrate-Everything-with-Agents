# SDK 起步

公开导入入口为 `oea`。以下是最小使用模式；扩展接口时读取当前安装包的签名、源码或对应版本的正式文档，不猜测方法名。配置字段可通过 `agents.schema` 查询，MCP 操作参数通过单项 `platform_guide` 查询。

## 连接已有宿主

控制端复用 `PlatformClient`，同一会话保持 consumer_id；重建连接时沿用它。执行中的 Agent 使用注入的 RunContext，不创建管理客户端绕过权限。

```python
from oea import Agent, PlatformClient

async def run_saved(agent_id, session_id, goal, consumer_id):
    async with PlatformClient.from_data_dir(consumer_id=consumer_id) as client:
        async with Agent.from_saved(
            agent_id, client=client, session_id=session_id
        ) as agent:
            return await agent.arun(goal)
```

`Agent.from_saved` 读取已保存配置。普通 `Agent(...)` 构造器声明并保存传入配置，不能用默认构造器代替读取。

## 独立 SDK 生命周期

使用已获准的模型配置；密钥保留在环境或秘密引用中。独立 SDK 默认使用标准用户目录下自己的隔离状态，不占用已运行主宿主的目录。

```python
from oea import Agent, Model

with Agent(
    name="example",
    model=Model.from_env(),
    instructions="Complete the request and retain verifiable evidence.",
) as agent:
    instance = agent.instance("example-session")
    result = instance.run("Summarize the supplied text: a bounded example.")
    print(result.text)
```

构造和进入上下文不启动宿主；首次使用才初始化。关闭生命周期会关闭其拥有的资源，保留持久回执。文件、命令及其他工具须通过 ToolKit 明确绑定；实际调用仍受当前授权和根预算约束。工作目录、临时数据和输出放在源码及同步目录之外。
