# 上游业务 MCP

1. 用 mcp.list/get 读取当前项目服务，mcp.schema 查看配置。保存使用 mcp.save 和 expected_revision。
2. stdio 指定命令，streamable_http 指定 URL；环境秘密和 HTTP header 仅保存命名 secret references。平台控制连接与上游业务连接分开。
3. mcp.discover 只初始化目标连接，返回实际工具名、模型别名和 schema。mcp.tools 读取已保存发现，不重连或执行业务。
4. Agent tools.mcp 绑定 server_id 和所需 upstream tool names，保存 Agent 修订。每个 Run 固定服务与工具版本；新版本通过显式恢复刷新能力。
5. 实际调用检查项目、Run 策略、原配置和当前授权。发现到的工具不自动获得执行权；平台控制 MCP 和两个控制工具禁止桥接为业务能力。
6. 调用超时或断线先读取原动作并核对外部结果。MCP 取消不证明远端副作用已停止，迟到结果归原回执；不换新键重放写操作。
7. 控制端明确试运行使用 mcp.call 和稳定调用键；mcp.call_result 读取同一意图的原回执，mcp.cancel_call 只请求取消。结果未知时先核对外部证据，再用 mcp.confirm_call 登记确认，不自动重发，也不为试运行创建虚构 Agent。

默认 session_scope=consumer，使每个 Agent 实例和控制端对话拥有独立的上游会话；只有明确需要共享业务状态时选择 shared。allowed_tools 是服务级授权，Agent 绑定和 Run 策略只能收窄；expose_to_codex 控制是否允许新的外部试运行。撤权不妨碍调用方读取自己的原回执。连接复用不提供并发容量准入。

mcp.discover 成功只证明连接和工具发现。真实业务验证需明确材料、目的地、授权与预期结果。
