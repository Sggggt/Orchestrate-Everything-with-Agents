/* Shared Skill registration and bounded Markdown previews. */
export function createSkillIntegration({ state, escape, icon, markdown, rpc, toast, render, loadBootstrap, showRemove }) {
  const call=(name,params={})=>rpc("skills."+name,{project_id:state.project.id,...params});
  const view=row=>({...row.definition,directory:row.definition.path,revision:String(row.revision),bound_agents:row.bound_agents||[],metadata:row.metadata||{}});
  const frontmatter = /^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/;
  function page() {
    const detail = state.skillDetail;
    if (detail) {
      const source = state.skillSource || "";
      const body = state.skillFile === "SKILL.md" ? source.replace(frontmatter, "") : source;
      return `<section class="panel-page mcp-page"><div class="page-heading"><div><button class="text-link" data-action="registry-skill-back">← Skill 集成</button><div class="skill-detail-title"><h1>${escape(detail.name)}</h1><span class="badge ${detail.enabled ? "" : "DISABLED"}">${detail.enabled ? "已启用" : "已禁用"}</span></div><p>${escape(detail.description)}</p></div><div class="heading-actions"><button class="icon-btn" data-action="registry-skill-refresh" data-id="${escape(detail.id)}" title="刷新目录" aria-label="刷新目录">${icon("refresh")}</button><button class="btn" data-action="registry-skill-toggle" data-id="${escape(detail.id)}">${detail.enabled ? "禁用" : "启用"}</button><button class="icon-btn danger-text" data-action="registry-skill-delete" data-id="${escape(detail.id)}" title="删除 ${escape(detail.name)}" aria-label="删除 ${escape(detail.name)}">${icon("trash")}</button></div></div><div class="form-section"><div class="document-foot"><span>${escape(detail.directory)}</span></div><p class="form-hint">关联 Agent：${detail.bound_agents.map(escape).join("、") || "尚未关联"} · 版本 ${escape(String(detail.revision).slice(0, 12))}</p>${detail.metadata.license ? `<p class="form-hint">许可证：${escape(detail.metadata.license)}</p>` : ""}${detail.metadata.compatibility ? `<p class="form-hint">环境要求：${escape(detail.metadata.compatibility)}</p>` : ""}<div class="skill-picker">${["SKILL.md", ...(state.skillFolder ? [".."] : []), ...detail.resources.filter(path=>path!=="SKILL.md")].map(path => `<button data-action="registry-skill-file" data-path="${escape(path)}" class="${state.skillFile === path ? "active" : ""}">${escape(path)}</button>`).join("")}</div><article class="markdown skill-markdown">${markdown(body)}</article>${state.skillOffset!=null?'<button class="btn" data-action="registry-skill-read-more">继续读取</button>':""}${state.skillFilesCursor?'<button class="btn" data-action="registry-skill-more-files">更多参考文件</button>':""}</div></section>`;
    }
    const registry = state.skillRegistry || { items: [] };
    const items = registry.items.filter(s => `${s.name} ${s.description}`.toLowerCase().includes((state.skillQuery || "").toLowerCase()));
    return `<section class="integration-directory"><header class="directory-bar"><h2>技能</h2><label class="library-search">${icon("search")}<input type="search" id="skill-registry-search" data-focus="skill-registry-search" value="${escape(state.skillQuery||"")}" aria-label="搜索技能" placeholder="搜索技能"></label></header>
    <details class="register-directory"><summary>${icon("folder")}从目录注册</summary><form id="skill-registry-form" class="directory-registration-row"><label class="field">目录路径<input id="skill-directory" data-focus="skill-directory" value="${escape(state.skillDirectory||"")}" placeholder="含 SKILL.md 的目录" required></label><button class="btn primary">注册</button></form></details>
    <div class="library-grid">${items.map(s=>`<article class="library-row"><button class="library-entry skill-card-title" data-action="registry-skill-select" data-id="${escape(s.id)}"><span class="library-glyph pigment-ochre">${icon("book")}</span><strong>${escape(s.name)}</strong></button>
      <span class="library-indicator ${s.enabled?"is-ready":""}" role="img" title="${s.enabled?"已启用":"已禁用"}" aria-label="${s.enabled?"已启用":"已禁用"}"></span>
      <details class="entry-menu"><summary aria-label="${escape(s.name)} · 更多操作">${icon("more")}</summary><div class="entry-menu-list">
      <button data-action="registry-skill-select" data-id="${escape(s.id)}">${icon("book")}查看技能</button>
      <button class="danger-text" data-action="registry-skill-delete" data-id="${escape(s.id)}" aria-label="删除 ${escape(s.name)}">${icon("trash")}删除</button></div></details></article>`).join("")||`<div class="library-empty">${icon("book")}<h2>${state.skillQuery?"没有匹配的技能":"暂无技能"}</h2></div>`}</div>${registry.has_more?'<button class="text-link" data-action="registry-skill-more">加载更多</button>':""}</section>`;
  }
  async function select(id) {
    const epoch=++state.skillEpoch, project=state.project.id;
    const [saved, file, files] = await Promise.all([call("describe",{skill_id:id}),call("read",{skill_id:id}),call("files",{skill_id:id,limit:25})]);
    if(state.skillEpoch!==epoch||state.project.id!==project)return;
    const detail={...view(saved),resources:files.items.map(item=>item.path)};
    Object.assign(state,{skillDetail:detail,skillSource:file.text,skillFile:"SKILL.md",skillOffset:file.next_offset,skillSha:file.sha256,skillFolder:"",skillFiles:files.items,skillFilesCursor:files.after});
    render();
  }
  async function action(name, element) {
    if (!name.startsWith("registry-skill-")) return false;
    const id = element.dataset.id || state.skillDetail?.id;
    if (name === "registry-skill-select") await select(id);
    if (name === "registry-skill-back") { state.skillEpoch++; state.skillDetail = null; render(); }
    if (name === "registry-skill-file") {
      const path=element.dataset.path;
      const entry=state.skillFiles.find(item=>item.path===path);
      if(path===".."||entry?.directory)await files(id,path===".."?state.skillFolder.replaceAll("\\","/").split("/").slice(0,-1).join("/"):path);
      else await read(id,path);
    }
    if(name==="registry-skill-read-more")await read(id,state.skillFile,true);
    if(name==="registry-skill-more-files")await files(id,state.skillFolder,true);
    if (name === "registry-skill-refresh") {
      await call("refresh",{skill_id:id,expected_revision:Number(state.skillDetail.revision)}); await loadBootstrap(); await select(id); toast("Skill 已刷新");
    }
    if (name === "registry-skill-toggle") {
      await call("set_enabled",{skill_id:id,enabled:!state.skillDetail.enabled,expected_revision:Number(state.skillDetail.revision)}); await loadBootstrap(); await select(id);
    }
    if (name === "registry-skill-delete") {
      const saved=state.skillRegistry.items.find(item=>item.id===id)||(state.skillDetail?.id===id?state.skillDetail:null);
      if(!saved)throw new Error("Skill 目录已变化，请重新加载。");
      showRemove(saved,async()=>{await call("remove",{skill_id:id,expected_revision:Number(saved.revision)});if(state.skillDetail?.id===id){state.skillEpoch++;state.skillDetail=null;}await loadBootstrap();render();toast("Skill 已删除，历史和源文件已保留");});
    }
    if (name === "registry-skill-more") {
      const page = await call("list",{cursor:state.skillRegistry.cursor,limit:25});
      const next={items:page.items.map(view),cursor:page.cursor,has_more:page.cursor!=null};
      state.skillRegistry = { ...next, items: [...state.skillRegistry.items, ...next.items] }; render();
    }
    return true;
  }
  async function read(id,path,append=false){
    const epoch=state.skillEpoch, detail=state.skillDetail;
    const page=await call("read",{skill_id:id,path,revision:Number(detail.revision),offset:append?state.skillOffset:0,limit:8192});
    if(state.skillEpoch!==epoch||state.skillDetail!==detail)return;
    if(append&&state.skillSha!==page.sha256)throw new Error("参考文件已变化，请重新读取。");
    Object.assign(state,{skillFile:path,skillSource:append?state.skillSource+page.text:page.text,skillOffset:page.next_offset,skillSha:page.sha256});render();
  }
  async function files(id,path,append=false){
    const epoch=state.skillEpoch, detail=state.skillDetail;
    const page=await call("files",{skill_id:id,path,after:append?state.skillFilesCursor:"",limit:25});
    if(state.skillEpoch!==epoch||state.skillDetail!==detail)return;
    state.skillFolder=path;state.skillFiles=append?[...state.skillFiles,...page.items]:page.items;state.skillFilesCursor=page.after;detail.resources=state.skillFiles.map(item=>item.path);render();
  }
  function input(e) {
    if (e.id === "skill-directory") state.skillDirectory = e.value;
    if (e.id === "skill-registry-search") { state.skillQuery = e.value; render(); }
  }
  async function submit(event) {
    if (event.target.id !== "skill-registry-form") return false;
    const directory = document.querySelector("#skill-directory").value.trim();
    const saved = await call("register_directory",{directory});
    const detail=saved.definition;
    state.skillDirectory = ""; await loadBootstrap(); await select(detail.id); toast("Skill 已注册，可为 Agent 关联");
    return true;
  }
  return { page, action, input, submit };
}
