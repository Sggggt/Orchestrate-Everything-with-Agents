# 受管 LangGraph

LangGraph 组织单个执行端内部的图。跨 Agent 调度、并发、回执和恢复归 Runtime；不建立额外 Agent 池。

```python
from agent.langgraph import GraphContext, LangGraphAgent, graph_wait, managed_tool_node
from langgraph.graph import StateGraph

builder = StateGraph(State, context_schema=GraphContext)
# 模型节点持久保存 pending_calls（含稳定 id），再进入工具节点。
builder.add_node("tools", managed_tool_node)
graph = builder.compile(checkpointer=checkpointer)
adapter = LangGraphAgent(
    "worker", graph, input_builder=lambda request: request.payload,
    managed_context=True, recursion_limit=24,
)
```

节点通过 runtime.context.oea 获得本次 RunContext。list_tools 提供统一定义，llm.complete 使用平台配置；上下文、客户端、取消事件和秘密不进入 state/configurable/checkpoint。

managed_tool_node 顺序执行 pending_calls，用 task_id 和持久 call id 去重，返回 tool_results 与模型 messages（包括图片引用）。模型节点同时将 LLMResponse.call_id 存入 model_call_id，工具节点据此关联真实请求。messages 的 reducer 由图定义；不要在同一节点重新生成模型调用 ID 再执行副作用。

派发、工具和等待使用独立节点。派发节点保存子任务 ID，等待节点调用 graph_wait({"kind":"tasks", "task_ids":[...], "policy":"ALL_SETTLED"})。message/input 等待使用对应 kind；指导请求先保存 report_issue 返回的 wait，再在等待节点 graph_wait。普通 interrupt 仍等待用户输入。

适配器通过 Command(resume=...) 恢复同一 task，注入新的 GraphContext。输入恢复带 Runtime 当前 wait_token；不要把 run_id 用作外部动作键。

持久检查点使用外部状态目录中的 SQLite checkpointer（langgraph-checkpoint-sqlite 可选依赖），由同一宿主拥有；内存 saver 仅用于测试。图与 Runtime 使用不同数据库文件。

图行为变化时显式更新适配器 graph_version；Runtime 使用既有 adapter_version 校验拒绝不兼容检查点，不能以更换任务键绕过迁移。

需要分支时显式提供 fork_builder，从完整、无待执行节点的检查点提取业务状态；适配器会移除运行控制、待执行工具和旧 model_call_id，再通过 checkpointer 写入新线程，不重跑原节点。未提供选择器的图不声明 supports_fork。使用异步持久 saver 的进程可用 `oea.stdio.run_agent_async` 在同一事件循环内管理 saver 生命周期。

取消传播到图协程、模型和工具。阻塞节点须合作取消或移到受管进程。managed_context 声明部分工具中介，不把任意 Python 图节点描述成系统沙箱；旧图默认维持 tool_mediation=none。
