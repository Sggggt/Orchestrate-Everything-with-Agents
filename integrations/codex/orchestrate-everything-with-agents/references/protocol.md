# 协议 3 约定

共享注册表是参数、默认值、Schema 和错误的唯一来源。platform_guide 查看一个 area/action，platform_call 执行一个 operation；两者都使用稳定 consumer_id。

长结果通过 results.read 读取：保留 result_ref、原 project_id，选择一个 JSON Pointer，按 next_offset 继续。引用属于原 consumer；跨对话不共享。配置固定 revision，发生 SOURCE_VERSION_CHANGED 时读取新配置并说明变化。读取引用永远不重放写操作。

同一意图使用原 idempotency_key。参数不同的新工作需要新的明确意图，未知结果不属于新工作。读回原 Run、WorkflowRun、action、model attempt 或产物之后，才决定修复和继续。

状态、保存、投递、阅读游标、工作完成和用户业务核验分别表达不同事实。保留完整内容和来源，用简短摘要展示，不为 Markdown 格式发起模型修复。

报告和产物可使用授权原件下载，不必反复读取小片段。命令输出通过原 command_session_id 查询或下载，未知状态不重放。

未发布文件可通过 artifacts.candidates 查看补丁回执或按需观察本次输出目录；artifacts.candidate 可复核当前来源版本。候选、明确发布与业务验收是不同事实；来源变化后只复核关联结论，不重放原写入。
