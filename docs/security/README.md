# security 模块接口

本机秘密存储和宿主目录锁；执行权限仍由服务边界核验。

OAP 应用凭据与管理凭据独立。Host 只保存应用凭据的 SHA-256 校验值，创建和轮换时返回凭据一次。
应用、绑定和会话的持久关系决定访问范围，名称、目录前缀和调用者提供的 ID 不授予权限。
应用业务对象由 OAP 读取；平台控制注册表和浏览器独立下载、事件入口均核验来源。
工具执行检查当前授权，应用调用禁用平台通信与委派；可信代码扩展必须单独显式授权。
详情见 [应用接入与 OAP](../applications/README.md)。

浏览器登录由已有控制凭据授权的一次性票据建立，首页访问和来源 Header 不授予管理 Cookie。
票据只保留哈希，60 秒有效且只能使用一次。首次引入 OAP 的离线升级在新目标目录轮换控制凭据，
保留模型秘密、原状态与源目录；客户端连接升级后的目标目录即可读取新连接材料。

源码：[src/oea/security/](../../src/oea/security/)

## Python 接口

- `security/lock.py`：`WorkspaceLock`
  - `acquire(self)`
  - `release(self)`
- `security/secrets.py`：`atomic_write(path: Path, data: bytes)`
- `security/secrets.py`：`dpapi(data: bytes, decrypt=False)`
- `security/secrets.py`：`SecretVault`
  - `get(self, agent_id: str)`
  - `contains_secret(self, value)`
  - `set(self, agent_id: str, value: str)`
  - `redact(self, value)`

## 依赖与边界

依赖模块：contracts。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
