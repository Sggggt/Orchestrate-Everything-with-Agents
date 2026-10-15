# 按需协议参考

只有主技能和当前子技能不足以确定参数、结果或错误处理时读取。

## 两个入口

```text
platform_guide(area="overview", action=null)
platform_call(area="...", action="...", params={...})
```

服务名是 `oea`；实际工具前缀以当前工具列表为准。guide 无 action 时只给功能索引，有 action 时只给该操作的参数与关联 schema。不要把标准模式的 platform_describe 或 runtime_call 当成当前紧凑模式工具。

0.3 起提供 projects 管理、summary_markdown 展示和实时摘要；具体动作仍按 guide 查询。三个 MCP 资源分别是 `oea://tool-index`（小型动作索引）、`oea://projects`（可见项目与目录）、`oea://output-contract`（摘要/完整内容约定）。更新宿主与 MCP 桥后重新发现资源，不把旧客户端缓存当作新版本协议。

guide 和 call 都返回 `ok`。`ok=true` 的小结果位于 `result`；`ok=false` 读取 `error.code / safe_message`。上游 MCP 结果可能还含 `isError=true`，不能仅看外层 ok 判断业务成功。

## 大结果

响应超过字符预算时返回 `result_ref`、preview、characters 和到期时间。preview 可能在 JSON 中间截断，只是提示，不能当成完整对象。使用：

```json
{"area":"control","action":"read_result","params":{
  "result_ref":"上一条响应的引用", "pointer":"/schemas/AgentConfig/properties/mcp_bindings",
  "offset":0, "limit":4000
}}
```

pointer 是 JSON Pointer；对象键里的 `/` 编码成 `~1`，`~` 编码成 `~0`。数组用 `/0` 等索引。`text` 是选中值的 JSON 文本片段，next_offset 为 null 才完整。列表结果可直接选 `/0`，分页结果可选 `/items/0`。查询 schemas 时需要被引用类型，才读取相应 `$defs`。

缓存仅在当前 MCP 进程内保留，默认 10 分钟、总上限 8 MiB。RESULT_EXPIRED 或过大未缓存时，对读取操作缩小范围重查；对写操作先核对持久记录，绝不为了恢复输出再执行写操作。

## 幂等、状态与授权

一个逻辑动作一个幂等键；网络重试原动作时参数与键保持一致。修改语义才生成新键。sessions.create / threads.create / fork / 文件上传没有调用方幂等键，未知结果先核对现有对象。

主要状态：QUEUED、RUNNING、WAITING、SUCCEEDED、FAILED、CANCELLED、SKIPPED、UNCERTAIN。WAITING 释放执行槽；UNCERTAIN 保留需要核对的边界。WAIT_TIMEOUT 是客户端等待结束，不表示任务失败。

平台宿主和 MCP 桥必须使用同一数据目录，认证由桥内部读取 client-token；模型不用知道该值。不得在用户可见的命令或日志里展开 token。紧凑模式只减少接口与输出规模，不把管理员调用自动变成某个 Agent 的权限上下文。

字段结构以当前 guide 为准；业务执行端的完整协议按需查询 development.request / descriptor / event，SDK 代码与文档位置见 local-instance.md。
