# 安装与宿主连接

1. 安装匹配平台的预编译 wheel 与需要的 web/mcp/workbench extras；普通安装不需要编译器，核对 oea --version。虚拟环境、状态和构建输出放在源码外。
2. 新状态用 `oea serve --allow-commands` 开放已绑定的命令和进程能力；不需要命令时省略该参数。Windows 的 Start-Workspace 启动脚本默认携带它，PowerShell 可用 `-AllowCommands:$false` 关闭。浏览器使用显示的 loopback 地址。
3. 已有宿主用 PlatformClient.from_data_dir；MCP 用 `oea mcp`。它只连接已有宿主，不创建第二个 Host。
4. 首先核对 /api/protocol 或 host.status。版本不符明确停止混用，不增加兼容开关。模型配置和连接状态分别验证。
5. 升级先只读盘点活动工作、原回执和未决效果。备份原状态及秘密材料，离线转换到新目录，核对 ID、引用、哈希、目录和累计用量后再安排明确授权的切换。
6. 原数据和工作目录不擅自移动、清空或覆盖。新版产生工作后，回滚也先核对新增事实，不能用旧库抹去新结果。

`host.status.allow_commands=false` 或 `agents.check` 的 `*:host_grant` 表示宿主授权未开放；Agent 配置保存成功不改变它。命令拒绝中的 `continuation=request_user_authorization` 提醒 Codex 核对已有授权；缺少授权时向用户说明命令用途、所需目录和重启影响并申请。已有明确授权无需重复询问。复用宿主不更新启动授权，先核对活动工作和未决动作，再在已授权范围内安排重启；不停止仍有工作的宿主，不另开同状态目录的 Host。

本机路径、实例 ID、业务连接和迁移清单留在本地配置与私有过程目录，不写入公共 Skill。

协议 2 的已识别状态用 `oea upgrade-state --data-dir SOURCE` 检查，停止宿主后用 `--target NEW_TARGET --apply` 转换到新目录。旧总输入上限保留原口径，旧图与未知效果保留供核对；不重放旧执行，不原地升级。遗留 schema 29 使用 `oea migrate --source SOURCE`。
