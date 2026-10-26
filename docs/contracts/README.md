# contracts 模块接口

共享配置、领域类型、错误与 Schema 校验。构造和导入不启动宿主。

源码：[src/oea/contracts/](../../src/oea/contracts/)

## Python 接口

- `contracts/configuration.py`：`Configuration`
- `contracts/configuration.py`：`Model`
  - `credential_name(cls, value)`
  - `endpoint(cls, value)`
  - `effective_window(self)`
  - `from_env(cls, *, prefix='OPENAI', **options)`
- `contracts/configuration.py`：`Instructions`
- `contracts/configuration.py`：`MCPBinding`
- `contracts/configuration.py`：`Tools`
  - `distinct(self)`
- `contracts/configuration.py`：`SkillBinding`
- `contracts/configuration.py`：`ContextPolicy`
- `contracts/configuration.py`：`Workspace`
- `contracts/configuration.py`：`Budget`
- `contracts/configuration.py`：`Delivery`
  - `valid_schema(cls, value)`
- `contracts/configuration.py`：`Execution`
  - `driver(self)`
- `contracts/configuration.py`：`CapabilityUse`
  - `consistent(self)`
- `contracts/configuration.py`：`UsePolicy`
- `contracts/configuration.py`：`Communication`
- `contracts/configuration.py`：`Manager`
- `contracts/configuration.py`：`AgentDefinition`
  - `unique_skills(self)`
- `contracts/configuration.py`：`manager_snapshot(definition)`
- `contracts/domain.py`：`Artifact`
  - `execution_owner(self)`
- `contracts/domain.py`：`RunResult`
- `contracts/domain.py`：`RunHandle`
- `contracts/domain.py`：`RunSpec`
  - `unique_artifacts(cls, value)`
  - `bounded_input(cls, value)`
- `contracts/domain.py`：`MessageSpec`
- `contracts/domain.py`：`AgentDescriptor`
- `contracts/domain.py`：`Wait`
- `contracts/domain.py`：`Completion`
- `contracts/errors.py`：`execution_state(state, error=None)`
- `contracts/errors.py`：`RuntimeFault`
  - `as_dict(self)`
- `contracts/errors.py`：`AdapterFailure`
- `contracts/errors.py`：`require(condition, code: str, message: str, **kwargs)`
- `contracts/schema_validation.py`：`check_schema(schema)`
- `contracts/schema_validation.py`：`schema_validator(schema)`
- `contracts/schema_validation.py`：`validate_value(validator, value)`

## 依赖与边界

依赖模块：标准库及声明的第三方依赖。

跨模块调用使用明确的 Python 对象和注入的服务；HTTP、MCP 可调用操作以共享操作注册表为准。执行权限由实际服务边界校验。

## 计量、内容与诊断

`contracts/measurement.py` 定义统一事实：`metric`、`value`、`unit`、`basis`。边界项另含 `limit`、`limit_source`、`policy_revision`、`decision`。basis 可为 measured、tokenizer_estimate、heuristic_estimate、provider_reported、unknown；未知值用 null，未知限制的 decision 为 unknown。

`content_view` 区分 full、excerpt、page、summarized，另带 integrity、source_ref、source_complete、range 和 next_offset。range 的 unit 与顶层 offset_unit 一致：文本与 JSON 页为 unicode_codepoint，产物页为 byte；调用方必须使用返回的续读位置。页面读取完毕不代表原始输出完整。partial、truncated、incomplete 分别表示中断后的部分内容、捕获上限截断和生成未完整结束。

RuntimeFault 保留 cause，并公开 origin、stage、purpose、reason、action_id、attempt_id、measurement、evidence_ref、continuation。`diagnostic` 是普通函数从这些事实生成的展示投影，不再由各入口猜测原因。provider_accepted 未获明确证据时保持 null，进入发送、收到响应和计费用量不能互相代替。
