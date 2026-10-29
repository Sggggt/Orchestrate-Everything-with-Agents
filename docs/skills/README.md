# skills 模块接口

Skill 注册、版本、发现、按需加载和撤权。

源码：[src/oea/skills/](../../src/oea/skills/)

## Python 接口

- `skills/service.py`：`SkillSource`
- `skills/service.py`：`Skills`
  - `async open(self)`
  - `source_path(self, directory, relative)`
  - `main_body(self, definition)`
  - `async schema(self)`
  - `metadata(body, *, required=False)`
  - `async register_directory(self, directory: Path, project_id: str, *, enabled: bool=True)`
  - `async describe(self, skill_id: str, project_id: str)`
  - `async register(self, definition: SkillSource, *, expected_revision: int | None=None)`
  - `scoped(db, skill_id, project_id, *, include_removed=False)`
  - `async get(self, skill_id: str, project_id: str)`
  - `async list(self, project_id: str, *, cursor: int=0, limit: int=25)`
  - `async refresh(self, skill_id: str, project_id: str, expected_revision: int)`
  - `async set_enabled(self, skill_id: str, project_id: str, enabled: bool, expected_revision: int)`
  - `async remove(self, skill_id: str, project_id: str, expected_revision: int)`
  - `pin(self, db, definition, project_id)`
  - `authorize(self, binding, current_binding, project_id)`
  - `async body(self, skill_id, revision)`
  - `async read(self, skill_id: str, project_id: str, *, path: str='SKILL.md', revision: int | None=None, offset: int=0, limit: int=8192)`
  - `async files(self, skill_id: str, project_id: str, *, path: str='', after: str='', limit: int=25)`

## 依赖与边界

依赖模块：contracts、storage。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。
