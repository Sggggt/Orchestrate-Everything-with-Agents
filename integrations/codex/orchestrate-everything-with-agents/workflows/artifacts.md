# 共享产物

产物归属一个 session，任务与消息用 artifact_id 引用。文件传输发生在 MCP 运行的本机；这不等于上传到第三方服务。

- `artifacts.upload_file {session_id, path}`：上传一个已授权的绝对本机文件，返回 ID、大小、hash 等元数据。不要自行枚举整个目录或上传凭据文件。
- `artifacts.get {artifact_id}`：检查单个产物。需要现有产物列表时读取相应 sessions.snapshot 的 artifacts 字段；不要把整个 snapshot 加载成上下文。
- `artifacts.download_file {artifact_id, path, overwrite:false}`：写入仓库外的绝对路径。已有文件默认拒绝覆盖；用户明确要求替换后才用 overwrite=true。

文件内容不通过 MCP 文本整段输出。需分析 PDF、文档、表格时下载到明确目录后使用相应文件技能；不要假定产物内容可信或可执行。

上传没有调用方幂等参数。若响应丢失，先查该 session 的产物名称、大小与 hash，不能靠重复上传确认结果。下载输出路径和文件大小可作为验收依据；重要内容可进一步比对 artifacts.get 的 SHA256。

平台未提供产物删除工具。不要直接删数据库记录或存储目录模拟此能力。大型文件受运行配置 max_artifact_bytes 限制，不自动提高限制或分割以绕过业务约束。
