# 插件与 Agent 创建

1. 固定项目与修订，用 plugins.list/get 查看插件、组件归属及实际操作。用 platform_guide 只发现当前动作，不加载完整协议。
2. plugins.install 接受目录、ZIP、固定提交的 Git 来源或官方 source。保留原幂等键，未知结果先读取原登记。安装成功不等于已连接或已授予 Agent。
3. 用 plugins.bind 绑定能力；Agent 工厂另需 creation.enabled、允许的 model_refs、plugins 和显式 recursive 授权。平台内调用 create_agent，再用 create_instance/assign_work 开始实际工作；沿原根预算，没有新增预算机制。
4. plugins.build 组合已有 MCP/Skill 为本机引用包，不修改源配置。plugins.export 生成可移植配置模板；接收方补全连接和凭据。密钥、原路径、业务数据和过程材料不得加入包。
5. 编辑插件引用的 MCP 使用 plugins.configure_component，首次保存自动另建专用连接。独立登记通过原 mcp 操作管理。卸载保留第三方原件与历史。
6. 外部控制 MCP 配置在外部宿主，使用已认证的共享平台操作；不得作为 OEA 内部上游自注册。OAP 应用不因此取得管理或协作权限。
