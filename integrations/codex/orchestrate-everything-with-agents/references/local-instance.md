# 本机定位

运行中的实例通过 `platform_call(area="control", action="status", params={})` 返回 URL 和数据目录。不要假定 CLI 默认目录就是一键启动器使用的目录。

此文件在安装技能时补充本机已核验的源码、解释器和数据目录，仅用于维护或开发定位；不存放认证值。源码中的通用版本不绑定个人用户名。

开发参考位于平台源码 `docs/sdk.md`、`examples/integration_agent.py`、`agent/base.py`、`agent/stdio_sdk.py`。只有选中开发流程时才读取必要部分。
