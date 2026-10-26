# security 模块接口

本机秘密存储和宿主目录锁；执行权限仍由服务边界核验。

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
