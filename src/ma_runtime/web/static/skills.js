/* Shared Skill registration and bounded Markdown previews. */
export function createSkillIntegration({ state, escape, icon, markdown, api, toast, render, loadBootstrap }) {
  const endpoint = (id) => `/skills/${encodeURIComponent(id)}`;
  const frontmatter = /^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/;
  function page() {
    const detail = state.skillDetail;
    if (detail) {
      const source = state.skillSource || "";
      const body = state.skillFile === "SKILL.md" ? source.replace(frontmatter, "") : source;
      return `<section class="panel-page mcp-page"><div class="page-heading"><div><button class="text-link" data-action="registry-skill-back">← Skill 集成</button><h1>${escape(detail.name)}</h1><p>${escape(detail.description)}</p></div><div class="heading-actions"><button class="btn" data-action="registry-skill-refresh" data-id="${escape(detail.id)}">${icon("refresh")}刷新目录</button><button class="btn" data-action="registry-skill-toggle" data-id="${escape(detail.id)}">${detail.enabled ? "禁用" : "启用"}</button><button class="btn danger" data-action="registry-skill-delete" data-id="${escape(detail.id)}" ${detail.bound_agents.length ? "disabled" : ""}>解除注册</button></div></div><div class="form-section"><div class="document-foot"><span>${escape(detail.directory)}</span><span class="badge">${detail.enabled ? "已启用" : "已禁用"}</span></div><p class="form-hint">关联 Agent：${detail.bound_agents.map(escape).join("、") || "尚未关联"} · 版本 ${escape(detail.revision.slice(0, 12))}</p>${detail.metadata.license ? `<p class="form-hint">许可证：${escape(detail.metadata.license)}</p>` : ""}${detail.metadata.compatibility ? `<p class="form-hint">环境要求：${escape(detail.metadata.compatibility)}</p>` : ""}<div class="skill-picker">${["SKILL.md", ...detail.resources].map(path => `<button data-action="registry-skill-file" data-path="${escape(path)}" class="${state.skillFile === path ? "active" : ""}">${escape(path)}</button>`).join("")}</div><article class="markdown skill-markdown">${markdown(body)}</article></div></section>`;
    }
    const registry = state.skillRegistry || { items: [] };
    const items = registry.items.filter(s => `${s.name} ${s.description}`.toLowerCase().includes((state.skillQuery || "").toLowerCase()));
    return `<section class="panel-page mcp-page"><div class="page-heading"><div><p class="eyebrow">SHARED KNOWLEDGE. FOCUSED EXECUTION.</p><h1>Skills</h1><p>注册操作知识 → 阅读技能说明 → 在 Agent 配置中关联。</p></div></div><form id="skill-registry-form" class="form-section directory-registration"><div class="section-heading"><div><h2>注册 Skill 目录</h2><p class="form-hint">选择含 SKILL.md 的目录。支持项目内或外部目录，编辑源文件后刷新即可更新。</p></div></div><div class="directory-registration-row"><label class="field">目录路径<input id="skill-directory" data-focus="skill-directory" value="${escape(state.skillDirectory || "")}" placeholder="Skill 目录的绝对路径" required></label><button class="btn primary" type="submit">${icon("plus")}注册 Skill</button></div></form><div class="filter-toolbar"><h2 class="compact-heading">技能目录 · ${registry.items.length}</h2><label class="inline-search">${icon("search")}<input id="skill-registry-search" data-focus="skill-registry-search" value="${escape(state.skillQuery || "")}" placeholder="搜索技能…" aria-label="搜索技能"></label></div><div class="integration-grid">${items.map(s => `<button class="integration-card skill-registry-card" data-action="registry-skill-select" data-id="${escape(s.id)}"><span class="integration-avatar">${icon("book")}</span><h3>${escape(s.name)}</h3><p>${escape(s.description)}</p><div class="document-foot"><span class="badge">${s.enabled ? "已启用" : "已禁用"}</span><span>${s.bound_agents.length} 个 Agent · ${s.resource_count} 个参考文件</span></div></button>`).join("") || `<div class="empty">${icon("book")}<h3>添加可复用的操作知识</h3><div>注册标准 Skill 目录后，可在 Agent 配置中关联。</div></div>`}</div>${registry.has_more ? '<button class="btn" data-action="registry-skill-more">加载更多</button>' : ""}</section>`;
  }
  async function select(id) {
    const [detail, file] = await Promise.all([api(endpoint(id)), api(endpoint(id) + "/read")]);
    Object.assign(state, { skillDetail: detail, skillSource: file.content, skillFile: "SKILL.md", route: "skills" });
    render();
  }
  async function action(name, element) {
    if (!name.startsWith("registry-skill-") && name !== "mcp-directory-register") return false;
    const id = element.dataset.id || state.skillDetail?.id;
    if (name === "registry-skill-select") await select(id);
    if (name === "registry-skill-back") { state.skillDetail = null; render(); }
    if (name === "registry-skill-file") {
      const file = await api(endpoint(id) + "/read?path=" + encodeURIComponent(element.dataset.path));
      state.skillFile = element.dataset.path; state.skillSource = file.content; render();
    }
    if (name === "registry-skill-refresh") {
      await api(endpoint(id) + "/refresh", { method: "POST", body: {} }); await loadBootstrap(); await select(id); toast("Skill 已刷新，新执行将使用新版本");
    }
    if (name === "registry-skill-toggle") {
      await api(endpoint(id), { method: "PATCH", body: { enabled: !state.skillDetail.enabled } }); await loadBootstrap(); await select(id);
    }
    if (name === "registry-skill-delete") {
      await api(endpoint(id), { method: "DELETE", body: {} }); state.skillDetail = null; await loadBootstrap(); render(); toast("已解除注册，源目录已保留");
    }
    if (name === "registry-skill-more") {
      const next = await api(`/skills?cursor=${encodeURIComponent(state.skillRegistry.cursor)}&limit=200`);
      state.skillRegistry = { ...next, items: [...state.skillRegistry.items, ...next.items] }; render();
    }
    if (name === "mcp-directory-register") {
      const directory = document.querySelector("#mcp-directory").value.trim();
      if (!directory) throw new Error("填写 MCP 目录路径");
      await api("/mcp/register-directory", { method: "POST", body: { directory } }); await loadBootstrap(); render(); toast("MCP 目录已注册");
    }
    return true;
  }
  function input(e) {
    if (e.id === "skill-directory") state.skillDirectory = e.value;
    if (e.id === "skill-registry-search") { state.skillQuery = e.value; render(); }
  }
  async function submit(event) {
    if (event.target.id !== "skill-registry-form") return false;
    const directory = document.querySelector("#skill-directory").value.trim();
    const detail = await api("/skills/register-directory", { method: "POST", body: { directory } });
    state.skillDirectory = ""; await loadBootstrap(); await select(detail.id); toast("Skill 已注册，可为 Agent 关联");
    return true;
  }
  return { page, action, input, submit };
}
