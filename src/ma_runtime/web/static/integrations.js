import { createLaunchEditor } from "./launch.mjs";
import { content, schemaView, valueEditor, readEditor, toolResult } from "./presentation.mjs";
import { createSkillIntegration } from "./skills.js";
/* MCP registry and per-Agent profile editors. No external browser dependencies. */
export function createIntegrationUI({
  state,
  escape,
  icon,
  badge,
  markdown,
  api,
  toast,
  render,
  renderModal,
  idempotency,
  loadBootstrap,
  time,
}) {
  const serviceLaunch = createLaunchEditor({
    scope: "mcp", getDraft: () => state.mcpDraft,
    read: draft => draft.command ? [draft.command, ...(draft.args || [])] : [],
    write: (draft, command) => { draft.command = command[0]; draft.args = command.slice(1); state.mcpDirty = true; },
    escape, icon, render, defaultMode: "program",
    purpose: "这里决定平台如何启动本机 MCP 服务。连接后可发现工具；实际调用在工具试运行或 Agent 任务中进行。",
  });
  const skillUI = createSkillIntegration({ state, escape, icon, markdown, api, toast, render, loadBootstrap });
  const server = (id) => state.mcpServers.find((s) => s.id === id);
  const mcpFields = [
    "id",
    "name",
    "description",
    "source_directory",
    "transport",
    "command",
    "args",
    "cwd",
    "url",
    "enabled",
    "expose_to_codex",
    "session_scope",
    "allowed_tools",
    "startup_timeout_seconds",
    "timeout_seconds",
    "max_concurrency",
    "max_connections",
    "max_output_bytes",
    "persist_results",
  ];
  const json = (value) => JSON.stringify(value, null, 2);
  const tabs = (items, current, action) =>
    `<nav class="profile-tabs" aria-label="配置分页">${items.map(([key, title, symbol]) => `<button type="button" class="profile-tab ${current === key ? "active" : ""}" data-action="${action}" data-tab="${key}" data-focus="${action}-${key}" aria-current="${current === key ? "page" : "false"}">${icon(symbol)}${title}</button>`).join("")}</nav>`;
  const input = (
    label,
    name,
    value,
    {
      type = "text",
      full = false,
      hint = "",
      placeholder = "",
      readonly = false,
    } = {},
  ) =>
    `<label class="field ${full ? "full" : ""}">${label}<input data-mcp="${name}" data-focus="mcp-${name}" type="${type}" value="${escape(value ?? "")}" placeholder="${escape(placeholder)}" ${readonly ? "readonly" : ""} ${["id", "name"].includes(name) ? "required" : ""}>${hint ? `<small>${hint}</small>` : ""}</label>`;
  const textarea = (label, name, value, hint = "", extra = "") =>
    `<label class="field full">${label}<textarea class="code-editor ${extra}" data-mcp="${name}" data-focus="mcp-${name}" spellcheck="false">${escape(value || "")}</textarea>${hint ? `<small>${hint}</small>` : ""}</label>`;
  function defaultMcp() {
    return {
      id: "",
      name: "",
      description: "",
      transport: "stdio",
      command: "",
      args: [],
      cwd: "",
      url: "",
      enabled: true,
      expose_to_codex: true,
      session_scope: "consumer",
      allowed_tools: null,
      allowed_text: "",
      startup_timeout_seconds: 30,
      timeout_seconds: 60,
      max_concurrency: 1,
      max_connections: 16,
      max_output_bytes: 524288,
      persist_results: false,
      env_text: "",
      headers_text: "",
    };
  }
  function parseObject(value, label) {
    let result;
    try {
      result = JSON.parse(value);
    } catch {
      throw new Error(`${label}需要填写有效的 JSON。`);
    }
    if (!result || Array.isArray(result) || typeof result !== "object")
      throw new Error(`${label}需要是 JSON 对象。`);
    return result;
  }
  function mcpCards() {
    const query = state.mcpQuery.trim().toLocaleLowerCase();
    const items = state.mcpServers.filter((s) =>
      `${s.name} ${s.id} ${s.description}`.toLocaleLowerCase().includes(query),
    );
    return items.length
      ? `<div class="mcp-grid">${items.map((s) => `<article class="mcp-card"><div class="mcp-card-head"><span class="integration-avatar">${icon(s.transport === "stdio" ? "terminal" : "external")}</span><div><button class="mcp-name" data-action="mcp-select" data-id="${escape(s.id)}">${escape(s.name)}</button><span class="mcp-type">${s.transport === "stdio" ? "本地进程" : s.transport === "sse" ? "HTTP · SSE" : "Streamable HTTP"}</span></div>${badge(s.status)}</div><p class="mcp-description">${escape(s.description === "Imported from local Codex MCP configuration" ? "来自本机 Codex 配置，由平台统一管理与分配。" : s.description || "为 Agent 和 Codex 提供统一的工具能力。")}</p><div class="mcp-card-metrics"><span><strong>${s.tool_count}</strong> 个工具</span><span><strong>${s.bound_agents?.length || 0}</strong> 个 Agent</span><span>${s.session_scope === "shared" ? "共享会话" : "上下文隔离"}</span></div>${s.last_error ? `<div class="integration-error">${escape(s.last_error.safe_message)}</div>` : ""}<footer><button class="text-link" data-action="mcp-connect" data-id="${escape(s.id)}" ${!s.enabled || state.mcpPending === s.id ? "disabled" : ""}>${icon("refresh")}${state.mcpPending === s.id ? "连接中…" : s.status === "CONNECTED" ? "刷新工具" : "连接测试"}</button><button class="text-link" data-action="mcp-select" data-id="${escape(s.id)}">管理服务 ${icon("arrow")}</button></footer></article>`).join("")}</div>`
      : `<div class="empty">${icon("plug")}<h3>${query ? "没有匹配的 MCP 服务" : "把工具接进来，让整个团队都能用"}</h3><div>${query ? "试试服务名称或 ID。" : "注册本地或 HTTP 服务，也可以导入已有的 Codex MCP 配置。"}</div><button class="btn primary" data-action="mcp-new">${icon("plus")}注册 MCP 服务</button></div>`;
  }
  function mcpPage() {
    if (state.mcpDraft) return mcpEditor();
    return `<section class="panel-page mcp-page"><div class="page-heading"><div><p class="eyebrow">ONE CONNECTION. SHARED CAPABILITIES.</p><h1>MCP 工具</h1><p>注册服务 → 连接并发现工具 → 在 Agent 配置中授权使用。</p></div><div class="heading-actions"><button class="btn" data-action="mcp-import">${icon("download")}导入 Codex 配置</button><button class="btn primary" data-action="mcp-new">${icon("plus")}注册服务</button><button class="icon-btn" data-action="mcp-sdk" title="SDK 与 Codex 接入" aria-label="SDK 与 Codex 接入">${icon("terminal")}</button></div></div><form id="mcp-registry-form" class="form-section directory-registration"><div class="section-heading"><div><h2>注册 MCP 目录</h2><p class="form-hint">选择含 mcp.json 的目录。连接配置在目录中维护，凭据在注册后单独设置。</p></div></div><div class="directory-registration-row"><label class="field">目录路径<input id="mcp-directory" data-focus="mcp-directory" value="${escape(state.mcpDirectory || "")}" placeholder="含 mcp.json 的绝对目录路径" required></label><button class="btn primary" type="submit">${icon("plus")}注册 MCP</button></div></form><div class="integration-stats">${[
      ["已注册服务", state.mcpServers.length],
      [
        "已连接",
        state.mcpServers.filter((s) => s.status === "CONNECTED").length,
      ],
      ["已发现工具", state.mcpServers.reduce((n, s) => n + s.tool_count, 0)],
      ["活跃调用", state.mcpServers.reduce((n, s) => n + s.active_calls, 0)],
    ]
      .map(
        ([label, n]) => `<div><span>${label}</span><strong>${n}</strong></div>`,
      )
      .join(
        "",
      )}</div><div class="filter-toolbar"><h2 class="compact-heading">服务目录</h2><label class="inline-search">${icon("search")}<input id="mcp-search" data-focus="mcp-search" aria-label="搜索 MCP 服务" placeholder="搜索服务…" value="${escape(state.mcpQuery)}"></label></div><div id="mcp-cards">${mcpCards()}</div></section>`;
  }
  function mcpEditor() {
    const d = state.mcpDraft,
      saved = state.mcpDetail;
    const content =
      state.mcpTab === "tools"
        ? mcpTools()
        : state.mcpTab === "calls"
          ? mcpHistory()
          : mcpConnectionForm(d, saved);
    return `<section class="panel-page mcp-page"><button class="text-link back-link" data-action="mcp-home">← 返回服务目录</button><div class="page-heading"><div><p class="eyebrow">MCP SERVER</p><h1>${escape(state.mcpId ? d.name : "连接一个新服务")}</h1><p>${state.mcpId ? `${escape(d.id)} · ${d.transport === "stdio" ? "本地进程" : "HTTP 服务"}` : "保存连接配置后，即可发现工具并分配给 Agent。"}</p></div>${state.mcpId ? `<div class="heading-actions">${badge(saved?.status || "DISCONNECTED")}<button class="btn" data-action="mcp-connect" data-id="${escape(state.mcpId)}" ${!d.enabled || state.mcpPending ? "disabled" : ""}>${icon("refresh")}${state.mcpPending ? "连接中…" : "连接并发现工具"}</button></div>` : ""}</div>${tabs(
      [
        ["connection", "连接配置", "settings"],
        ["tools", `可用工具 ${saved?.tool_count || 0}`, "plug"],
        ["calls", "调用记录", "activity"],
      ],
      state.mcpTab,
      "mcp-tab",
    )}${saved?.last_error ? `<div class="integration-error">${escape(saved.last_error.safe_message)}<small>${escape(saved.last_error.code)}</small></div>` : ""}${content}</section>`;
  }
  function mcpConnectionForm(d, saved) {
    return `<form id="mcp-form" class="integration-form"><section class="form-section"><h3>服务信息</h3><div class="form-grid">${input("显示名称", "name", d.name, { placeholder: "例如：文档工具" })}${input("服务 ID", "id", d.id, { placeholder: "小写字母、数字和连字符", readonly: !!state.mcpId })}${input("服务描述", "description", d.description, { full: true, placeholder: "这个服务提供什么能力？" })}<label class="field">连接方式<select data-mcp="transport" data-focus="mcp-transport"><option value="stdio" ${d.transport === "stdio" ? "selected" : ""}>STDIO · 本地服务</option><option value="streamable-http" ${d.transport === "streamable-http" ? "selected" : ""}>Streamable HTTP</option><option value="sse" ${d.transport === "sse" ? "selected" : ""}>HTTP + SSE · 兼容模式</option></select></label><label class="field">会话共享方式<select data-mcp="session_scope" data-focus="mcp-session_scope"><option value="consumer" ${d.session_scope === "consumer" ? "selected" : ""}>按调用方隔离（推荐）</option><option value="shared" ${d.session_scope === "shared" ? "selected" : ""}>共享同一个会话</option></select><small>有当前项目或浏览器状态的服务，推荐隔离；服务配置仍由所有 Agent 共用。</small></label></div></section>${d.transport === "stdio" ? serviceLaunch.panel(d) : ""}<section class="form-section"><h3>${d.transport === "stdio" ? "运行目录与环境变量" : "连接服务地址"}</h3><div class="form-grid">${d.transport === "stdio" ? `${input("工作目录（可选）", "cwd", d.cwd, { full: true, placeholder: "本机目录的绝对路径" })}<div class="field full"><span>环境变量</span><small>${saved?.env_keys?.length ? "已保存：" + escape(saved.env_keys.join("、")) + "。" : ""}保留已保存的值；编辑后将整组替换并加密保存。</small>${valueEditor(JSON.parse(d.env_text || "{}"), "mcp-env", { type: "object", additionalProperties: { type: "string", writeOnly: true } })}<button type="button" class="text-link danger-text" data-action="mcp-clear-secrets" data-secret="env">清空已保存的环境变量</button></div>` : `${input("MCP 服务 URL", "url", d.url, { full: true, type: "url", placeholder: "https://your-service/mcp" })}<div class="field full"><span>认证及请求头</span><small>${saved?.header_keys?.length ? "已保存：" + escape(saved.header_keys.join("、")) + "。" : ""}编辑后将整组替换并加密保存；敏感值不会回传浏览器。</small>${valueEditor(JSON.parse(d.headers_text || "{}"), "mcp-headers", { type: "object", additionalProperties: { type: "string", writeOnly: true } })}<button type="button" class="text-link danger-text" data-action="mcp-clear-secrets" data-secret="headers">清空已保存的请求头</button></div>`}</div></section><details class="form-section form-advanced"><summary>访问与运行高级设置</summary><div class="form-grid">${input("启动超时（秒）", "startup_timeout_seconds", d.startup_timeout_seconds, { type: "number" })}${input("工具调用超时（秒）", "timeout_seconds", d.timeout_seconds, { type: "number" })}${input("每个连接的并发调用", "max_concurrency", d.max_concurrency, { type: "number" })}${input("最多保留的连接数", "max_connections", d.max_connections, { type: "number" })}${textarea("工具允许列表（可选）", "allowed_text", d.allowed_text, "每行一个原始工具名称；留空表示允许服务发现的全部工具。Agent 还可以选择更小的工具范围。")}</div><label class="toggle-row"><span>启用服务<small>关闭后不再建立连接或接受新的调用。</small></span><input type="checkbox" data-mcp="enabled" ${d.enabled ? "checked" : ""}></label><label class="toggle-row"><span>允许 Codex 与外部 SDK 调用<small>通过平台接口共用服务；仍需平台的本机访问凭据。</small></span><input type="checkbox" data-mcp="expose_to_codex" ${d.expose_to_codex ? "checked" : ""}></label><label class="toggle-row"><span>在 MCP 调用历史中持久保存结果<small>默认仅保留调用元数据。Agent 的对话与执行记录由其运行时另行保存。</small></span><input type="checkbox" data-mcp="persist_results" ${d.persist_results ? "checked" : ""}></label></details><footer class="settings-footer"><div class="inline-actions">${state.mcpId ? `<button type="button" class="btn small" data-action="mcp-disconnect" data-id="${escape(state.mcpId)}">断开连接</button><button type="button" class="text-link danger-text" data-action="mcp-delete-confirm" data-id="${escape(state.mcpId)}">移除服务</button>` : '<span class="form-hint">凭据加密保存 · 配置独立于源码</span>'}</div><button class="btn primary" type="submit" ${state.mcpSaving ? "disabled" : ""}>${icon("check")}${state.mcpSaving ? "保存中…" : "保存服务"}</button></footer></form>`;
  }
  function mcpTools() {
    const tools = state.mcpDetail?.catalog?.tools || [];
    if (!state.mcpId)
      return `<div class="empty"><h3>先保存连接配置</h3><div>服务注册后，可以连接并发现它提供的工具。</div></div>`;
    return `<div class="section-heading"><h2>工具目录 <span class="section-count">${tools.length}</span></h2><button class="btn small" data-action="mcp-connect" data-id="${escape(state.mcpId)}" ${state.mcpPending ? "disabled" : ""}>${icon("refresh")}刷新发现</button></div>${
      tools.length
        ? `<div class="tool-catalog">${tools
            .map((t) => {
              const allowed =
                state.mcpDetail.allowed_tools == null ||
                state.mcpDetail.allowed_tools.includes(t.name);
              return `<article class="tool-card"><div class="tool-card-head"><span class="tool-symbol">${icon("terminal")}</span><h3>${escape(t.name)}</h3><span class="badge ${allowed ? "" : "DISABLED"}">${!allowed ? "未开放" : t.annotations?.readOnlyHint ? "只读提示" : "可执行"}</span><button class="btn small" data-action="mcp-test-tool" data-tool="${escape(t.name)}" ${!allowed || !state.mcpDetail.expose_to_codex ? "disabled" : ""}>试运行 ${icon("arrow")}</button></div>${content(t.description || "此工具未提供描述。") }<details><summary>参数协议</summary>${schemaView(t.inputSchema || {})}</details></article>`;
            })
            .join("")}</div>`
        : `<div class="empty">${icon("plug")}<h3>工具会在连接后出现在这里</h3><div>发现工具只读取服务能力，不执行具体业务操作。</div></div>`
    }`;
  }
  function mcpHistory() {
    return `<div class="section-heading"><h2>最近调用</h2><button class="btn small" data-action="mcp-refresh-calls">${icon("refresh")}刷新</button></div>${state.mcpCalls.length ? `<div class="mcp-history">${state.mcpCalls.map((c) => `<article><span class="tool-symbol">${icon("terminal")}</span><div><strong>${escape(c.tool_name)}</strong><small>${escape(c.consumer_id)}</small>${c.error ? `<small class="danger-text">${escape(c.error.code)}</small>` : ""}</div>${badge(c.state)}<span class="call-duration">${c.duration_ms == null ? "执行中" : c.duration_ms + " ms"}<small>${time(c.started_at)}</small></span></article>`).join("")}</div>` : `<div class="empty">${icon("activity")}<h3>每次调用，都有来处与结果</h3><div>来自 Agent、Codex 和 SDK 的调用会显示在这里。</div></div>`}`;
  }
  function profileTabs() {
    const d = state.settingsDraft;
    return tabs(
      [
        ["general", "基础配置", "settings"],
        ["instructions", "工作指令", "file"],
        ["skills", `Skills ${(d.skills || []).length + (d.skill_bindings || []).length}`, "book"],
        ["mcp", `MCP 工具 ${(d.mcp_bindings || []).length}`, "plug"],
      ],
      state.settingsTab,
      "profile-tab",
    );
  }
  function profilePanel() {
    const d = state.settingsDraft;
    d.skills ||= [];
    d.skill_bindings ||= [];
    d.mcp_bindings ||= [];
    if (state.settingsTab === "instructions")
      return `<section class="form-section document-section"><div class="section-heading"><div><h3>AGENT.md</h3><p class="form-hint">定义这个 Agent 的职责、工作方式与交付要求。每次执行都会加载。</p></div><div class="inline-actions"><button type="button" class="btn small" data-action="agent-import-md">${icon("download")}导入</button><button type="button" class="btn small" data-action="profile-preview">${icon("file")}${state.profilePreview ? "编辑" : "预览"}</button></div></div>${state.profilePreview ? `<div class="markdown-preview">${markdown(d.agent_md || "尚未填写 AGENT.md。")}</div>` : `<label class="field"><span class="sr-only">AGENT.md 内容</span><textarea class="code-editor document-editor" data-setting="agent_md" data-focus="setting-agent_md" placeholder="# 角色与目标\n\n描述这个 Agent 应该如何工作。" spellcheck="false">${escape(d.agent_md || "")}</textarea></label>`}<div class="document-foot"><span>AGENT.md</span><span>独立于其他 Agent · 保存后生效</span></div></section>`;
    if (state.settingsTab === "skills") return skillPanel();
    if (state.settingsTab === "mcp") return agentMcpPanel();
    return "";
  }
  function skillPanel() {
    const linked = state.settingsDraft.skill_bindings;
    const registry = state.skillRegistry?.items || [];
    return `<section class="form-section"><div class="section-heading"><div><h3>关联平台 Skill</h3><p class="form-hint">共享技能按需读入；关联不会授予 MCP 工具权限。派发任务时可进一步指定加载范围。</p></div><button type="button" class="btn small" data-action="route" data-route="skills">${icon("book")}管理 Skill</button></div><div class="binding-list">${registry.map(s => `<article class="binding-card ${linked.some(b => b.skill_id === s.id) ? "selected" : ""}"><label class="binding-header"><input type="checkbox" data-skill-binding="${escape(s.id)}" data-focus="skill-binding-${escape(s.id)}" ${linked.some(b => b.skill_id === s.id) ? "checked" : ""}><span class="integration-avatar">${icon("book")}</span><span><strong>${escape(s.name)}</strong><small>${escape(s.description)}</small></span><span class="badge">${s.enabled ? "已启用" : "已禁用"}</span></label><button type="button" class="text-link" data-action="registry-skill-select" data-id="${escape(s.id)}">查看 SKILL.md</button></article>`).join("") || '<p class="form-hint">先在 Skill 集成页注册目录，再为 Agent 关联。</p>'}</div></section>${state.settingsDraft.skills.length ? `<details class="form-section"><summary>已有内嵌技能 · ${state.settingsDraft.skills.length}</summary>${legacySkillPanel()}</details>` : ""}`;
  }
  function legacySkillPanel() {
    const skills = state.settingsDraft.skills;
    const index = Math.min(state.selectedSkill, Math.max(0, skills.length - 1)),
      skill = skills[index];
    return `<section class="form-section skill-section"><div class="section-heading"><div><h3>专属于这个 Agent 的技能</h3><p class="form-hint">先提供技能目录，执行时按需读取 SKILL.md，保持上下文专注。</p></div><button type="button" class="btn small" data-action="skill-add">${icon("plus")}添加技能</button></div>${skills.length ? `<div class="skill-picker">${skills.map((s, i) => `<button type="button" class="${index === i ? "active" : ""}" data-action="skill-select" data-index="${i}">${icon("book")}${escape(s.name || "未命名技能")}<span class="dot ${s.enabled ? "online" : "offline"}"></span></button>`).join("")}</div><div class="form-grid"><label class="field">技能名称<input data-skill="name" data-index="${index}" data-focus="skill-name-${index}" value="${escape(skill.name)}" required></label><label class="field">技能 ID<input data-skill="id" data-index="${index}" data-focus="skill-id-${index}" value="${escape(skill.id)}" pattern="[a-z](?:[a-z0-9_]|-){0,63}" required></label><label class="field full">何时使用<input data-skill="description" data-index="${index}" data-focus="skill-description-${index}" value="${escape(skill.description || "")}" placeholder="说明这项技能适用的任务，帮助 Agent 按需选择。"></label><label class="field full">SKILL.md<textarea class="code-editor document-editor" data-skill="content" data-index="${index}" data-focus="skill-content-${index}" spellcheck="false" placeholder="---\nname: my-skill\ndescription: 何时使用这个技能\n---\n\n# 操作步骤">${escape(skill.content || "")}</textarea></label></div><label class="toggle-row"><span>启用此技能<small>未启用的技能不会出现在执行上下文中。</small></span><input type="checkbox" data-skill="enabled" data-index="${index}" ${skill.enabled ? "checked" : ""}></label><div class="document-foot"><span>skills/${escape(skill.id || "skill-id")}/SKILL.md</span><div class="inline-actions"><button type="button" class="text-link" data-action="skill-preview" data-index="${index}">预览</button><button type="button" class="text-link" data-action="skill-import" data-index="${index}">导入 Markdown</button><button type="button" class="text-link danger-text" data-action="skill-remove" data-index="${index}">移除技能</button></div></div>${Object.keys(skill.resources || {}).length ? `<p class="form-hint">另含 ${Object.keys(skill.resources).length} 个参考文件，可通过 SDK 管理。</p>` : ""}` : `<div class="empty">${icon("book")}<h3>让经验成为可复用的技能</h3><div>为这个 Agent 添加操作步骤、领域规则或检查清单。</div><button type="button" class="btn" data-action="skill-add">添加第一个技能</button></div>`}</section>`;
  }
  function agentMcpPanel() {
    const bindings = state.settingsDraft.mcp_bindings;
    return `<section class="form-section"><div class="section-heading"><div><h3>选择这个 Agent 可用的工具</h3><p class="form-hint">服务由平台统一管理；这里的选择仅影响当前 Agent。</p></div><button type="button" class="btn small" data-action="profile-mcp-refresh">${icon("refresh")}刷新服务</button></div>${
      state.mcpServers.length
        ? `<div class="binding-list">${state.mcpServers
            .map((s) => {
              const b = bindings.find((x) => x.server_id === s.id);
              const tools = (state.mcpCatalogs[s.id]?.tools || []).filter(
                (t) =>
                  s.allowed_tools == null || s.allowed_tools.includes(t.name),
              );
              return `<article class="binding-card ${b ? "selected" : ""}"><label class="binding-header"><input type="checkbox" data-binding="${escape(s.id)}" data-focus="binding-${escape(s.id)}" ${b ? "checked" : ""}><span class="integration-avatar">${icon("plug")}</span><span><strong>${escape(s.name)}</strong><small>${s.tool_count} 个工具 · ${s.session_scope === "shared" ? "共享会话" : "按对话隔离"}</small></span>${badge(s.status)}</label>${b ? `<div class="binding-options"><label class="check-row"><input type="checkbox" data-binding-all="${escape(s.id)}" data-focus="binding-all-${escape(s.id)}" ${b.tools == null ? "checked" : ""}>使用此服务的全部已开放工具</label>${b.tools != null ? `<div class="binding-tools">${tools.length ? tools.map((t) => `<label class="check-row"><input type="checkbox" data-binding-tool="${escape(s.id)}" data-focus="binding-tool-${escape(s.id)}-${escape(t.name)}" data-tool="${escape(t.name)}" ${b.tools.includes(t.name) ? "checked" : ""}><span title="${escape(t.description || "")}">${escape(t.name)}</span></label>`).join("") : `<button type="button" class="btn small" data-action="binding-discover" data-id="${escape(s.id)}">连接并发现可选工具</button>`}</div>` : ""}</div>` : ""}</article>`;
            })
            .join("")}</div>`
        : `<div class="empty">${icon("plug")}<h3>先连接你的工具服务</h3><div>在 MCP 集成页注册服务后，就能在这里为 Agent 分配工具。</div><button type="button" class="btn" data-action="route" data-route="mcp">前往 MCP 集成</button></div>`
    }</section>`;
  }
  function agentDocumentLinks(a) {
    return `<div class="agent-doc-links"><button data-action="configure-agent" data-id="${escape(a.id)}" data-tab="instructions" title="编辑 ${escape(a.name)} 的 AGENT.md">${icon("file")}AGENT.md<span class="dot ${a.agent_md ? "online" : "offline"}"></span></button><button data-action="configure-agent" data-id="${escape(a.id)}" data-tab="skills">${icon("book")}Skills ${(a.skills || []).filter((s) => s.enabled).length + (a.skill_bindings || []).length}</button><button data-action="configure-agent" data-id="${escape(a.id)}" data-tab="mcp">${icon("plug")}MCP ${(a.mcp_bindings || []).length}</button></div>`;
  }
  function mcpModal(m) {
    if (m.type === "mcp-import")
      return {
        title: "导入已有的 MCP 配置",
        wide: false,
        content: `<p>从本机 Codex 读取已注册的服务，复制连接配置与凭据。已有服务会保留，不会被覆盖。</p>${m.loading ? '<div class="loading-inline">正在读取本机配置…</div>' : m.candidates?.length ? `<div class="import-candidates">${m.candidates.map((c) => `<label class="import-candidate"><input type="checkbox" data-import-mcp="${escape(c.name)}" ${c.registered ? "disabled" : ""} ${m.selected.includes(c.name) ? "checked" : ""}><span><strong>${escape(c.name)}</strong><small>${c.transport} ${c.has_credentials ? "· 含加密配置" : ""}</small></span>${c.registered ? '<span class="badge">已注册</span>' : ""}</label>`).join("")}</div>` : '<div class="empty"><h3>没有发现本机 Codex MCP 配置</h3><div>你仍可手动注册本地或 HTTP 服务。</div></div>'}${m.report ? `<div class="integration-error">${m.report.errors.map((e) => `<p>${escape(e.name)}：${escape(e.error.safe_message)}</p>`).join("")}</div>` : ""}`,
        footer: `<button class="btn" data-action="close-modal">取消</button><button class="btn primary" data-action="mcp-import-selected" ${m.loading || m.running ? "disabled" : ""}>${m.running ? "注册中…" : "导入并注册"}</button>`,
      };
    if (m.type === "mcp-delete")
      return {
        title: "移除 MCP 服务",
        content: `<p>移除 ${escape(server(m.id)?.name || m.id)} 的连接配置与保存的凭据。已有调用记录会保留。请先解除 Agent 对此服务的绑定。</p>`,
        footer: `<button class="btn" data-action="close-modal">取消</button><button class="btn danger" data-action="mcp-delete" data-id="${escape(m.id)}">移除服务</button>`,
      };
    if (m.type === "mcp-call")
      return {
        title: m.tool.name,
        wide: true,
        content: `${content(m.tool.description || "填写参数，试运行这个 MCP 工具。")}<div class="form-grid"><label class="field full">调用上下文<input id="mcp-consumer" data-focus="mcp-consumer" value="${escape(m.consumer || "console")}" placeholder="console"><small>同一上下文会保留服务中的项目选择等状态。</small></label><div class="field full"><span>调用参数</span>${valueEditor(JSON.parse(m.arguments), "mcp-arguments", m.tool.inputSchema)}</div></div><details class="tool-schema"><summary>查看参数协议</summary>${schemaView(m.tool.inputSchema || {})}</details>${m.error ? `<div class="integration-error">${escape(m.error)}</div>` : ""}${m.result ? `<div class="section-heading"><h2>返回结果</h2>${badge(m.result.isError ? "FAILED" : "SUCCEEDED")}</div><div class="tool-result rendered-result">${toolResult(m.result)}</div>` : ""}`,
        footer: `<button class="btn" data-action="close-modal">关闭</button><button class="btn primary" data-action="mcp-run-tool" ${m.running ? "disabled" : ""}>${icon("terminal")}${m.running ? "调用中…" : "运行工具"}</button>`,
      };
    if (m.type === "mcp-sdk")
      return {
        title: "连接 Codex 与开发 SDK",
        wide: true,
        content: m.loading
          ? '<div class="loading-inline">正在读取接入配置…</div>'
          : `<p>将以下配置合并到 Codex 的 config.toml。Codex 通过平台使用 MCP 服务，也可以注册服务、配置 Agent 与编排任务。</p><div class="markdown tool-result">${markdown("```toml\n" + m.text + "\n```")}</div><button class="btn small" data-action="copy-mcp-config">复制 Codex 配置</button><div class="section-heading"><h2>Python SDK</h2><a class="text-link" href="/api/protocol" target="_blank" rel="noopener">查看接口与 JSON Schema ${icon("external")}</a></div><div class="markdown tool-result">${markdown("```python\n" + m.sdk + "\n```")}</div><p class="form-hint">接口调用复用正在运行的平台。凭据从本机数据目录读取，无需复制到代码或聊天中。</p>`,
        footer: '<button class="btn" data-action="close-modal">完成</button>',
      };
    if (m.type === "skill-preview")
      return {
        title: "SKILL.md 预览",
        wide: true,
        content: `<div class="markdown-preview">${markdown(m.content)}</div>`,
        footer: '<button class="btn" data-action="close-modal">关闭</button>',
      };
    return null;
  }
  function draftFor(detail) {
    return {
      ...defaultMcp(),
      ...structuredClone(detail),
      allowed_text: (detail.allowed_tools || []).join("\n"),
      env_text: "",
      headers_text: "",
    };
  }
  async function selectMcp(id) {
    state.mcpId = id;
    state.mcpTab = "connection";
    state.mcpDirty = false;
    state.mcpDetail = server(id) || null;
    state.mcpDraft = draftFor(server(id) || { id });
    state.mcpCalls = [];
    const draft = state.mcpDraft;
    render();
    const detail = await api(`/mcp/servers/${encodeURIComponent(id)}`);
    if (state.mcpId === id && state.mcpDraft === draft) {
      state.mcpDetail = detail;
      state.mcpCatalogs[id] = detail.catalog || {};
      render();
    }
  }
  async function refreshCalls() {
    if (!state.mcpId) return;
    const id = state.mcpId;
    const rows = await api(`/mcp/calls?server_id=${encodeURIComponent(id)}`);
    if (state.mcpId === id) {
      state.mcpCalls = rows;
      render();
    }
  }
  async function discoverBinding(id) {
    state.mcpCatalogs[id] = await api(
      `/mcp/servers/${encodeURIComponent(id)}/tools`,
    );
    await loadBootstrap();
    render();
  }
  function exampleArguments(schema) {
    const result = {};
    for (const [key, field] of Object.entries(schema?.properties || {})) {
      if (!(schema.required || []).includes(key)) continue;
      result[key] =
        field.default ??
        field.enum?.[0] ??
        (field.type === "integer" || field.type === "number"
          ? field.minimum || 0
          : field.type === "boolean"
            ? false
            : field.type === "array"
              ? []
              : field.type === "object"
                ? {}
                : "");
    }
    return result;
  }
  function pickMarkdown() {
    return new Promise((resolve, reject) => {
      const chooser = document.createElement("input");
      chooser.type = "file";
      chooser.accept = ".md,.markdown,text/markdown,text/plain";
      chooser.oncancel = () => resolve(null);
      chooser.onchange = async () => {
        const file = chooser.files?.[0];
        if (!file) return resolve(null);
        if (file.size > 65536)
          return reject(new Error("Markdown 文档不能超过 64 KiB。"));
        try {
          resolve(await file.text());
        } catch {
          reject(new Error("无法读取这个文件。"));
        }
      };
      chooser.click();
    });
  }
  async function action(name, element) {
    if (serviceLaunch.action(name, element)) return true;
    if (name === "mcp-clear-secrets") {
      state.mcpDraft[element.dataset.secret === "env" ? "env_text" : "headers_text"] = "{}";
      state.mcpDirty = true; render(); toast("已标记清空，保存服务后生效"); return true;
    }
    if (await skillUI.action(name, element)) return true;
    const id = element.dataset.id;
    if (name === "configure-agent") {
      const a = state.agents.find((a) => a.id === id);
      state.settingsId = id;
      state.settingsDraft = { ...structuredClone(a), api_key: "" };
      state.settingsTab = element.dataset.tab || "general";
      state.selectedSkill = 0;
      state.profilePreview = true;
      state.route = "settings";
      state.modal = null;
      render();
    } else if (name === "profile-tab") {
      state.settingsTab = element.dataset.tab;
      state.profilePreview = true;
      render();
      if (state.settingsTab === "mcp") {
        for (const b of state.settingsDraft.mcp_bindings || []) {
          const known = server(b.server_id);
          if (known?.tool_count && !state.mcpCatalogs[b.server_id])
            await discoverBinding(b.server_id);
        }
      }
    } else if (name === "profile-preview") {
      state.profilePreview = !state.profilePreview;
      render();
    } else if (name === "agent-import-md") {
      const draft = state.settingsDraft;
      const content = await pickMarkdown();
      if (content != null && state.settingsDraft === draft) {
        draft.agent_md = content;
        state.profilePreview = true;
        render();
        toast("文档已载入，保存 Agent 后生效");
      }
    } else if (name === "skill-add") {
      state.settingsDraft.skills ||= [];
      let number = state.settingsDraft.skills.length + 1;
      while (state.settingsDraft.skills.some((s) => s.id === `skill-${number}`))
        number++;
      state.settingsDraft.skills.push({
        id: `skill-${number}`,
        name: `新技能 ${number}`,
        description: "",
        content: "# 工作步骤\n\n1. 理解任务目标。\n2. 执行并验证结果。\n",
        enabled: true,
        resources: {},
      });
      state.selectedSkill = state.settingsDraft.skills.length - 1;
      render();
    } else if (name === "skill-select") {
      state.selectedSkill = Number(element.dataset.index);
      render();
    } else if (name === "skill-remove") {
      state.settingsDraft.skills.splice(Number(element.dataset.index), 1);
      state.selectedSkill = 0;
      render();
    } else if (name === "skill-import") {
      const draft = state.settingsDraft,
        index = Number(element.dataset.index),
        skill = draft.skills[index];
      const text = await pickMarkdown();
      if (
        text != null &&
        state.settingsDraft === draft &&
        draft.skills[index] === skill
      ) {
        const parsed = await api("/skills/parse", {
          method: "POST",
          body: { skill_id: skill.id, markdown: text, enabled: skill.enabled },
        });
        if (state.settingsDraft === draft && draft.skills[index] === skill) {
          draft.skills[index] = { ...parsed, resources: skill.resources || {} };
          render();
          toast("技能已载入，保存 Agent 后生效");
        }
      }
    } else if (name === "skill-preview") {
      state.modal = {
        type: "skill-preview",
        content:
          state.settingsDraft.skills[Number(element.dataset.index)].content,
      };
      renderModal();
    } else if (name === "profile-mcp-refresh") {
      await loadBootstrap();
      render();
    } else if (name === "binding-discover") {
      await discoverBinding(id);
    } else if (name === "mcp-home") {
      state.mcpId = null;
      state.mcpDraft = null;
      state.mcpDetail = null;
      state.mcpTab = "connection";
      render();
    } else if (name === "mcp-new") {
      state.mcpId = null;
      state.mcpDraft = defaultMcp();
      state.mcpDetail = null;
      state.mcpCalls = [];
      state.mcpTab = "connection";
      state.mcpDirty = false;
      state.route = "mcp";
      state.modal = null;
      render();
    } else if (name === "mcp-select") {
      state.route = "mcp";
      state.modal = null;
      await selectMcp(id);
    } else if (name === "mcp-tab") {
      state.mcpTab = element.dataset.tab;
      render();
      if (state.mcpTab === "calls") await refreshCalls();
    } else if (name === "mcp-refresh-calls") {
      await refreshCalls();
    } else if (name === "mcp-connect") {
      if (state.mcpPending) return true;
      if (state.mcpId === id && state.mcpDirty)
        throw new Error("请先保存连接配置，再测试连接。");
      state.mcpPending = id;
      render();
      try {
        const detail = await api(
          `/mcp/servers/${encodeURIComponent(id)}/connect`,
          { method: "POST", body: { consumer_id: "console" } },
        );
        state.mcpCatalogs[id] = detail.catalog || {};
        if (state.mcpId === id) state.mcpDetail = detail;
        await loadBootstrap();
        toast(`已连接，发现 ${detail.tool_count} 个工具`);
      } finally {
        state.mcpPending = null;
        await loadBootstrap().catch(() => {});
        render();
      }
    } else if (name === "mcp-disconnect") {
      await api(`/mcp/servers/${encodeURIComponent(id)}/disconnect`, {
        method: "POST",
        body: {},
      });
      await loadBootstrap();
      if (state.mcpId === id)
        state.mcpDetail = await api(`/mcp/servers/${encodeURIComponent(id)}`);
      render();
      toast("连接已断开；后续调用会按需重新连接");
    } else if (name === "mcp-delete-confirm") {
      state.modal = { type: "mcp-delete", id };
      renderModal();
    } else if (name === "mcp-delete") {
      await api(`/mcp/servers/${encodeURIComponent(id)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
      });
      state.modal = null;
      state.mcpDraft = null;
      state.mcpId = null;
      await loadBootstrap();
      render();
      toast("MCP 服务已移除");
    } else if (name === "mcp-import") {
      const m = {
        type: "mcp-import",
        candidates: [],
        selected: [],
        loading: true,
      };
      state.modal = m;
      renderModal();
      try {
        m.candidates = await api("/mcp/codex-candidates");
      } finally {
        m.loading = false;
        if (state.modal === m) renderModal();
      }
    } else if (name === "mcp-import-selected") {
      const m = state.modal;
      if (!m.selected.length) throw new Error("请先选择需要注册的 MCP 服务。");
      if (m.running) return true;
      m.running = true;
      renderModal();
      try {
        m.report = await api("/mcp/import-codex", {
          method: "POST",
          body: { names: m.selected },
        });
        await loadBootstrap();
        if (state.modal === m && !m.report.errors.length) {
          state.modal = null;
          state.route = "mcp";
          state.mcpDraft = null;
        }
        toast(
          `已注册 ${m.report.imported.length} 个服务${m.report.skipped.length ? `，保留 ${m.report.skipped.length} 个已有服务` : ""}`,
        );
      } finally {
        m.running = false;
        render();
      }
    } else if (name === "mcp-test-tool") {
      const tool = state.mcpDetail?.catalog?.tools?.find(
        (t) => t.name === element.dataset.tool,
      );
      if (!tool) throw new Error("请先刷新工具目录。");
      state.modal = {
        type: "mcp-call",
        serverId: state.mcpId,
        tool,
        arguments: json(exampleArguments(tool.inputSchema)),
        consumer: "console",
        key: idempotency(),
        result: null,
      };
      renderModal();
    } else if (name === "mcp-run-tool") {
      const m = state.modal;
      if (m.running) return true;
      const args = readEditor(document.querySelector('[data-value-editor="mcp-arguments"]'));
      m.arguments = json(args);
      m.running = true;
      m.error = "";
      m.key ||= idempotency();
      renderModal();
      try {
        m.result = await api(
          `/mcp/servers/${encodeURIComponent(m.serverId)}/call`,
          {
            method: "POST",
            body: {
              tool_name: m.tool.name,
              arguments: args,
              consumer_id: m.consumer || "console",
              idempotency_key: m.key,
            },
          },
        );
        m.key = null;
      } catch (error) {
        m.error = error.message;
      } finally {
        m.running = false;
        if (state.modal === m) renderModal();
      }
    } else if (name === "mcp-sdk") {
      const m = { type: "mcp-sdk", loading: true };
      state.modal = m;
      renderModal();
      const servers = (await api("/mcp/codex-config")).mcp_servers;
      const serverName = servers.oea ? "oea" : "multi_agent";
      const config = servers[serverName];
      m.text = `[mcp_servers.${serverName}]\ncommand = ${json(config.command)}\nargs = ${JSON.stringify(config.args)}\nstartup_timeout_sec = ${config.startup_timeout_sec}\ntool_timeout_sec = ${config.tool_timeout_sec}\nenabled_tools = ${JSON.stringify(config.enabled_tools)}\nenv = { ${Object.entries(config.env).map(([key, value]) => `${json(key)} = ${json(value)}`).join(", ")} }`;
      m.sdk = `import asyncio\nfrom oea import PlatformClient\n\nasync def main():\n    async with PlatformClient.from_data_dir(${json(config.args.at(-1))}) as client:\n        servers = await client.mcp.list()\n        protocol = await client.protocol()\n        print(servers)\n\nasyncio.run(main())`;
      m.loading = false;
      if (state.modal === m) renderModal();
    } else if (name === "copy-mcp-config") {
      await navigator.clipboard.writeText(state.modal.text);
      toast("Codex 配置已复制");
    } else return false;
    return true;
  }
  function onInput(e) {
    serviceLaunch.input(e);
    skillUI.input(e);
    const editor = e.closest?.("[data-value-editor]");
    const field = { "mcp-env": "env_text", "mcp-headers": "headers_text" }[editor?.dataset.valueEditor];
    if (editor && (field || editor.dataset.valueEditor === "mcp-arguments")) {
      try {
        const value = json(readEditor(editor));
        if (field) { state.mcpDraft[field] = value; state.mcpDirty = true; }
        else { state.modal.arguments = value; state.modal.key = idempotency(); }
        editor.removeAttribute("data-invalid");
      } catch (error) { editor.dataset.invalid = error.message; }
    }
    if (e.id === "mcp-search") {
      state.mcpQuery = e.value;
      document.querySelector("#mcp-cards").innerHTML = mcpCards();
    }
    if (e.id === "mcp-directory") state.mcpDirectory = e.value;
    if (e.dataset.mcp && state.mcpDraft) {
      state.mcpDraft[e.dataset.mcp] =
        e.type === "checkbox"
          ? e.checked
          : e.type === "number"
            ? Number(e.value)
            : e.value;
      state.mcpDirty = true;
    }
    if (e.dataset.skill) {
      const skill = state.settingsDraft.skills[Number(e.dataset.index)];
      skill[e.dataset.skill] = e.type === "checkbox" ? e.checked : e.value;
    }
    if (e.id === "mcp-arguments" && state.modal?.type === "mcp-call") {
      state.modal.arguments = e.value;
      state.modal.key = idempotency();
    }
    if (e.id === "mcp-consumer" && state.modal?.type === "mcp-call")
      state.modal.consumer = e.value;
  }
  async function onChange(e) {
    serviceLaunch.change(e);
    if (e.dataset.skillBinding) {
      state.settingsDraft.skill_bindings ||= [];
      state.settingsDraft.skill_bindings = state.settingsDraft.skill_bindings.filter(b => b.skill_id !== e.dataset.skillBinding);
      if (e.checked) state.settingsDraft.skill_bindings.push({ skill_id: e.dataset.skillBinding });
      render();
    }
    if (e.dataset.mcp === "transport") render();
    if (e.dataset.importMcp && state.modal?.type === "mcp-import") {
      state.modal.selected = state.modal.selected.filter(
        (n) => n !== e.dataset.importMcp,
      );
      if (e.checked) state.modal.selected.push(e.dataset.importMcp);
    }
    const bindings = state.settingsDraft?.mcp_bindings;
    if (e.dataset.binding) {
      state.settingsDraft.mcp_bindings ||= [];
      state.settingsDraft.mcp_bindings =
        state.settingsDraft.mcp_bindings.filter(
          (b) => b.server_id !== e.dataset.binding,
        );
      if (e.checked)
        state.settingsDraft.mcp_bindings.push({
          server_id: e.dataset.binding,
          tools: null,
        });
      render();
    }
    if (e.dataset.bindingAll) {
      const b = bindings.find((b) => b.server_id === e.dataset.bindingAll);
      b.tools = e.checked ? null : [];
      render();
      if (!e.checked && !state.mcpCatalogs[b.server_id])
        await discoverBinding(b.server_id);
    }
    if (e.dataset.bindingTool) {
      const b = bindings.find((b) => b.server_id === e.dataset.bindingTool);
      b.tools = (b.tools || []).filter((name) => name !== e.dataset.tool);
      if (e.checked) b.tools.push(e.dataset.tool);
    }
  }
  async function submit(event) {
    if (await skillUI.submit(event)) return true;
    if (event.target.id === "mcp-registry-form") {
      const directory = document.querySelector("#mcp-directory").value.trim();
      if (!directory) throw new Error("填写 MCP 目录路径");
      await api("/mcp/register-directory", { method: "POST", body: { directory } });
      state.mcpDirectory = "";
      await loadBootstrap(); render(); toast("MCP 目录已注册");
      return true;
    }
    if (event.target.id !== "mcp-form") return false;
    if (state.mcpSaving) return true;
    const d = state.mcpDraft,
      config = {};
    for (const id of ["mcp-env", "mcp-headers"]) {
      const editor = document.querySelector(`[data-value-editor="${id}"]`);
      if (editor?.hasAttribute("data-invalid")) throw new Error(editor.dataset.invalid);
    }
    if (d.transport === "stdio") serviceLaunch.commit(d);
    for (const key of mcpFields) config[key] = d[key];
    config.args = d.transport === "stdio" ? d.args : [];
    if (
      !Array.isArray(config.args) ||
      config.args.some((a) => typeof a !== "string")
    )
      throw new Error("请检查本地服务的启动选项。");
    if (d.transport === "stdio") config.url = "";
    else {
      config.command = "";
      config.cwd = "";
    }
    config.allowed_tools = d.allowed_text.trim()
      ? d.allowed_text
          .trim()
          .split(/\r?\n/)
          .map((v) => v.trim())
          .filter(Boolean)
      : null;
    const secrets = {};
    if (d.env_text.trim()) secrets.env = parseObject(d.env_text, "环境变量");
    if (d.headers_text.trim())
      secrets.headers = parseObject(d.headers_text, "认证请求头");
    if (Object.keys(secrets).length) config.secret_values = secrets;
    if (!config.id || !config.name)
      throw new Error("请填写 MCP 服务 ID 与显示名称。");
    const submittedDraft = JSON.stringify(d);
    state.mcpSaving = true;
    render();
    try {
      const detail = await api(
        `/mcp/servers/${encodeURIComponent(config.id)}`,
        { method: "PUT", body: config },
      );
      if (state.mcpDraft === d && JSON.stringify(d) === submittedDraft) {
        state.mcpId = detail.id;
        state.mcpDetail = detail;
        state.mcpDraft = draftFor(detail);
        state.mcpDirty = false;
      }
      await loadBootstrap();
      toast("MCP 服务已保存，可以开始连接测试");
    } finally {
      state.mcpSaving = false;
      render();
    }
    return true;
  }
  return {
    skillPage: skillUI.page,
    mcpPage,
    profileTabs,
    profilePanel,
    agentDocumentLinks,
    modal: mcpModal,
    action,
    input: onInput,
    change: onChange,
    toggle: serviceLaunch.toggle,
    submit,
  };
}
