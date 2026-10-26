import { createLaunchEditor } from "./launch.mjs";
import { content, schemaView, valueEditor, readEditor } from "./presentation.mjs";

export function createMCPDirectory({state,escape,icon,badge,markdown,rpc,toast,render,loadBootstrap,showCredential,showSDK,showImport,showRemove,showToolTrial,time}) {
  const call=(name,params={})=>rpc("mcp."+name,{...(name==="save"?{}:{project_id:state.project.id}),...params});
  const json=value=>JSON.stringify(value,null,2);
  const server=id=>state.mcpServers.find(value=>value.id===id);
  const launch=createLaunchEditor({scope:"mcp",getDraft:()=>state.mcpDraft,read:d=>d.command?[d.command,...d.args]:[],write:(d,argv)=>{d.command=argv[0];d.args=argv.slice(1);state.mcpDirty=true;},escape,icon,render,defaultMode:"program",purpose:"这里决定平台如何启动本机 MCP 服务。连接后可发现工具；实际调用在 Agent 任务中进行。"});
  const view=row=>({...row.definition,name:row.definition.name||row.definition.id,command:row.definition.command?.[0]||"",args:row.definition.command?.slice(1)||[],status:({connected:"CONNECTED",connecting:"CONNECTING",disconnected:"DISCONNECTED"})[row.connection_state]||"DISCONNECTED",revision:row.revision,tool_count:row.tool_count,active_calls:row.active_calls||0,bound_agents:row.bound_agents||[],session_scope:row.definition.session_scope,allowed_tools:row.definition.allowed_tools,expose_to_codex:row.definition.expose_to_codex,env_keys:Object.keys(row.definition.environment_refs||{}),header_keys:Object.keys(row.definition.header_refs||{}),definition:row.definition});
  const draftFor=value=>({...value,env_text:"",headers_text:"",allowed_text:(value.allowed_tools||[]).join("\n")});
  const defaultMcp=()=>draftFor({id:"",name:"",description:"",transport:"stdio",command:"",args:[],cwd:"",url:"",enabled:true,expose_to_codex:true,session_scope:"consumer",allowed_tools:null,timeout_seconds:120,environment:{},environment_refs:{},header_refs:{},revision:0});
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
  function mcpCards() {
    const query = state.mcpQuery.trim().toLocaleLowerCase();
    const items = state.mcpServers.filter((s) =>
      `${s.name} ${s.id} ${s.description}`.toLocaleLowerCase().includes(query),
    );
    return items.length
      ? `<div class="mcp-grid">${items.map((s) => `<article class="mcp-card"><div class="mcp-card-head"><span class="integration-avatar">${icon(s.transport === "stdio" ? "terminal" : "external")}</span><div><button class="mcp-name" data-action="mcp-select" data-id="${escape(s.id)}">${escape(s.name)}</button><span class="mcp-type">${s.transport === "stdio" ? "本地进程" : s.transport === "sse" ? "HTTP · SSE" : "Streamable HTTP"}</span></div>${badge(s.status)}</div><p class="mcp-description">${escape(s.description === "Imported from local Codex MCP configuration" ? "" : s.description || "")}</p><div class="mcp-card-metrics"><span><strong>${s.tool_count??"—"}</strong> 个工具</span><span><strong>${s.bound_agents?.length || 0}</strong> 个 Agent</span><span>${s.session_scope === "shared" ? "共享会话" : "上下文隔离"}</span></div>${s.last_error ? `<div class="integration-error">${escape(s.last_error.safe_message)}</div>` : ""}<footer><button class="text-link" data-action="mcp-connect" data-id="${escape(s.id)}" ${!s.enabled || state.mcpPending === s.id ? "disabled" : ""}>${icon("refresh")}${state.mcpPending === s.id ? "连接中…" : s.status === "CONNECTED" ? "刷新工具" : "连接测试"}</button><button class="icon-btn" data-action="mcp-select" data-id="${escape(s.id)}" title="管理服务" aria-label="管理服务">${icon("settings")}</button></footer></article>`).join("")}</div>`
      : `<div class="empty">${icon("plug")}<h3>${query ? "没有匹配的 MCP 服务" : "暂无 MCP 服务"}</h3><div>${query ? "试试服务名称或 ID。" : "注册本地或 HTTP 服务，也可以导入已有的 Codex MCP 配置。"}</div><button class="icon-btn" data-action="mcp-new" title="注册服务" aria-label="注册服务">${icon("plus")}</button></div>`;
  }
  function mcpPage() {
    if (state.mcpDraft) return mcpEditor();
    return `<section class="panel-page mcp-page"><div class="page-heading"><div><h1>MCP 工具</h1></div><div class="heading-actions"><button class="icon-btn" data-action="mcp-import" title="导入 Codex 配置" aria-label="导入 Codex 配置">${icon("upload")}</button><button class="icon-btn" data-action="mcp-new" title="注册服务" aria-label="注册服务">${icon("plus")}</button><button class="icon-btn" data-action="mcp-sdk" title="SDK 与 Codex 接入" aria-label="SDK 与 Codex 接入">${icon("terminal")}</button></div></div><form id="mcp-registry-form" class="form-section directory-registration"><div class="section-heading"><div><h2>注册 MCP 目录</h2><p class="form-hint">选择含 mcp.json 的目录。连接配置在目录中维护，凭据在注册后单独设置。</p></div></div><div class="directory-registration-row"><label class="field">目录路径<input id="mcp-directory" data-focus="mcp-directory" value="${escape(state.mcpDirectory || "")}" placeholder="含 mcp.json 的绝对目录路径" required></label><button class="icon-btn" type="submit" title="注册 MCP" aria-label="注册 MCP">${icon("plus")}</button></div></form><div class="integration-stats">${[
      ["已注册服务", state.mcpServers.length],
      [
        "已连接",
        state.mcpServers.filter((s) => s.status === "CONNECTED").length,
      ],
      ["已发现工具", state.mcpServers.reduce((n, s) => n + (s.tool_count||0), 0)],
      ["活跃调用", state.mcpServers.reduce((n, s) => n + s.active_calls, 0)],
    ]
      .map(
        ([label, n]) => `<div><span>${label}</span><strong>${n}</strong></div>`,
      )
      .join(
        "",
      )}</div><div class="filter-toolbar"><h2 class="compact-heading">服务目录</h2><label class="inline-search">${icon("search")}<input id="mcp-search" data-focus="mcp-search" aria-label="搜索 MCP 服务" placeholder="搜索服务…" value="${escape(state.mcpQuery)}"></label></div><div id="mcp-cards">${mcpCards()}</div>${state.mcpCursor!=null?'<button class="btn load-more" data-action="mcp-load-more">加载更多服务</button>':""}</section>`;
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
  function mcpConnectionForm(d,saved) {
    return `<form id="mcp-form" class="integration-form"><section class="form-section"><h3>服务信息</h3><div class="form-grid">${input("显示名称","name",d.name,{placeholder:"例如：文档工具"})}${input("服务 ID","id",d.id,{placeholder:"小写字母、数字和连字符",readonly:!!state.mcpId})}${input("服务描述","description",d.description,{full:true,placeholder:"这个服务提供什么能力？"})}<label class="field">连接方式<select data-mcp="transport" data-focus="mcp-transport"><option value="stdio" ${d.transport==="stdio"?"selected":""}>STDIO · 本地服务</option><option value="streamable_http" ${d.transport==="streamable_http"?"selected":""}>Streamable HTTP</option></select></label><label class="field">会话共享方式<select data-mcp="session_scope" data-focus="mcp-session_scope"><option value="consumer" ${d.session_scope==="consumer"?"selected":""}>按调用方隔离（推荐）</option><option value="shared" ${d.session_scope==="shared"?"selected":""}>共享同一个会话</option></select><small>有当前项目或浏览器状态的服务，推荐隔离；服务配置仍由所有 Agent 共用。</small></label></div></section>${d.transport==="stdio"?launch.panel(d):""}<section class="form-section"><h3>${d.transport==="stdio"?"运行目录与环境变量":"连接服务地址"}</h3><div class="form-grid">${d.transport==="stdio"?`${input("工作目录（可选）","cwd",d.cwd,{full:true,placeholder:"本机目录的绝对路径"})}<div class="field full"><span>环境变量</span><small>${saved?.env_keys?.length?"已保存："+escape(saved.env_keys.join("、"))+"。":""}秘密值不会回传浏览器；留空保留已保存引用。</small>${valueEditor(JSON.parse(d.env_text||"{}"),"mcp-env",{type:"object",additionalProperties:{type:"string",writeOnly:true}})}</div>`:`${input("MCP 服务 URL","url",d.url,{full:true,type:"url",placeholder:"https://your-service/mcp"})}<div class="field full"><span>认证及请求头</span><small>${saved?.header_keys?.length?"已保存："+escape(saved.header_keys.join("、"))+"。":""}秘密值不会回传浏览器；留空保留已保存引用。</small>${valueEditor(JSON.parse(d.headers_text||"{}"),"mcp-headers",{type:"object",additionalProperties:{type:"string",writeOnly:true}})}</div>`}</div></section><details class="form-section form-advanced"><summary>访问与运行高级设置</summary><div class="form-grid">${input("工具调用超时（秒）","timeout_seconds",d.timeout_seconds,{type:"number"})}${textarea("工具允许列表（可选）","allowed_text",d.allowed_text,"每行一个原始工具名称；留空表示允许服务发现的全部工具。Agent 还可以选择更小的工具范围。")}</div><label class="toggle-row"><span>启用服务<small>关闭后不再建立连接或接受新的调用。</small></span><input type="checkbox" data-mcp="enabled" ${d.enabled?"checked":""}></label><label class="toggle-row"><span>允许 Codex 与外部 SDK 调用<small>通过平台接口共用服务；仍需平台的本机访问凭据。</small></span><input type="checkbox" data-mcp="expose_to_codex" ${d.expose_to_codex?"checked":""}></label></details><footer class="settings-footer"><div class="inline-actions">${state.mcpId?`<button type="button" class="btn small" data-action="mcp-disconnect" data-id="${escape(state.mcpId)}">断开连接</button><button type="button" class="text-link" data-action="mcp-credential">单独设置认证</button><button type="button" class="text-link danger-text" data-action="mcp-remove">移除服务</button>`:'<span class="form-hint">凭据加密保存 · 配置独立于源码</span>'}</div><button class="btn primary" type="submit" ${state.mcpSaving?"disabled":""}>${icon("check")}${state.mcpSaving?"保存中…":"保存服务"}</button></footer></form>`;
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
              return `<article class="tool-card"><div class="tool-card-head"><span class="tool-symbol">${icon("terminal")}</span><h3>${escape(t.name)}</h3><span class="badge ${allowed ? "" : "DISABLED"}">${!allowed ? "未开放" : t.annotations?.readOnlyHint ? "只读提示" : "可执行"}</span><button class="btn small" data-action="mcp-test-tool" data-tool="${escape(t.name)}" ${!allowed||!state.mcpDetail.expose_to_codex?"disabled":""}>试运行 ${icon("arrow")}</button></div>${content(t.description || "此工具未提供描述。") }<details><summary>参数协议</summary>${schemaView(t.inputSchema || {})}</details></article>`;
            })
            .join("")}</div>`
        : `<div class="empty">${icon("plug")}<h3>工具会在连接后出现在这里</h3><div>发现工具只读取服务能力，不执行具体业务操作。</div></div>`
    }`;
  }
  function mcpHistory() {
    return `<div class="section-heading"><h2>最近调用</h2><button class="icon-btn" data-action="mcp-refresh-calls" title="刷新" aria-label="刷新">${icon("refresh")}</button></div>${state.mcpCalls.length ? `<div class="mcp-history">${state.mcpCalls.map((c) => `<article><span class="tool-symbol">${icon("terminal")}</span><div><strong>${escape(c.tool_name)}</strong><small>${escape(c.consumer_id)}</small>${c.error ? `<small class="danger-text">${escape(c.error.code)}</small>` : ""}</div>${badge(c.state)}<span class="call-duration">${c.duration_ms == null ? "执行中" : c.duration_ms + " ms"}<small>${time(c.started_at)}</small></span></article>`).join("")}</div>` : `<div class="empty">${icon("activity")}<h3>暂无调用</h3><div>来自 Agent、Codex 和 SDK 的调用会显示在这里。</div></div>`}${state.mcpCallsCursor!=null?'<button class="btn load-more" data-action="mcp-more-calls">加载更早的调用</button>':""}`;
  }
  async function select(id) {
    const epoch=++state.mcpEpoch;
    const saved=await call("inspect",{server_id:id});
    if(epoch!==state.mcpEpoch)return;
    const detail=view(saved);
    Object.assign(state,{mcpId:id,mcpDetail:detail,mcpDraft:draftFor(detail),mcpTab:"connection",mcpDirty:false,mcpCalls:[],mcpCallsCursor:null});render();
  }
  async function toolsFor(id) {
    try {const catalog=await call("tools",{server_id:id});return {tools:catalog.tools.map(tool=>({name:tool.upstream_name,description:tool.spec.description,inputSchema:tool.spec.input_schema}))};}
    catch(error){if(error.code==="MCP_NOT_DISCOVERED")return {tools:[]};throw error;}
  }
  async function calls(more=false) {
    const id=state.mcpId,page=await call("calls",{server_id:id,limit:25,...(more?{cursor:state.mcpCallsCursor}:{})});
    if(state.mcpId!==id)return;
    const values=page.items.map(item=>({...item,tool_name:item.tool_name,consumer_id:item.consumer_id||item.instance_name,state:({completed:"SUCCEEDED",failed:"FAILED",needs_review:"UNCERTAIN",intent:"QUEUED",dispatched:"RUNNING"})[item.state],duration_ms:["completed","failed"].includes(item.state)?item.updated_at-item.created_at:null,started_at:item.created_at}));state.mcpCalls=more?[...state.mcpCalls,...values]:values;state.mcpCallsCursor=page.cursor;render();
  }
  async function action(name,element) {
    if(launch.action(name,element))return;
    const id=element.dataset.id||state.mcpId;
    if(name==="mcp-home"){state.mcpEpoch++;state.mcpDraft=null;state.mcpDetail=null;state.mcpId=null;render();}
    if(name==="mcp-new"){state.mcpEpoch++;Object.assign(state,{mcpId:null,mcpDraft:defaultMcp(),mcpDetail:null,mcpTab:"connection",mcpDirty:false});render();}
    if(name==="mcp-select")await select(id);
    if(name==="mcp-tab"){
      state.mcpTab=element.dataset.tab;
      if(state.mcpTab==="tools"&&state.mcpId){const catalog=await toolsFor(state.mcpId);state.mcpDetail.catalog=catalog;}
      if(state.mcpTab==="calls"&&state.mcpId)await calls();
      render();
    }
    if(name==="mcp-refresh-calls")await calls();
    if(name==="mcp-more-calls"&&state.mcpCallsCursor!=null)await calls(true);
    if(name==="mcp-load-more"){await loadBootstrap(true);render();}
    if(name==="mcp-import")await showImport();
    if(name==="mcp-remove")showRemove(state.mcpDetail,async()=>{state.mcpEpoch++;state.mcpDraft=null;state.mcpDetail=null;state.mcpId=null;await loadBootstrap();render();});
    if(name==="mcp-connect"){
      if(state.mcpPending)return;
      if(state.mcpId===id&&state.mcpDirty)throw new Error("请先保存连接配置，再测试连接。");
      state.mcpPending=id;render();
      try{await call("discover",{server_id:id});await loadBootstrap();if(state.mcpId===id){const detail=view(await call("inspect",{server_id:id}));detail.catalog=await toolsFor(id);state.mcpDetail=detail;}toast("已连接并发现工具");}
      finally{state.mcpPending=null;render();}
    }
    if(name==="mcp-disconnect"){await call("disconnect",{server_id:id});await loadBootstrap();if(state.mcpId===id)state.mcpDetail=view(await call("inspect",{server_id:id}));render();toast("连接已断开；后续调用会按需重新连接");}
    if(name==="mcp-test-tool"){
      const tool=state.mcpDetail?.catalog?.tools?.find(value=>value.name===element.dataset.tool);
      if(!tool)throw new Error("请先刷新工具目录。");
      await showToolTrial(state.mcpId,tool);
    }
    if(name==="mcp-credential")await showCredential(state.mcpDetail,async()=>{await select(id);await loadBootstrap();});
    if(name==="mcp-sdk")await showSDK();
  }
  function onInput(e){
    launch.input(e);
    const editor=e.closest("[data-value-editor]"),field={"mcp-env":"env_text","mcp-headers":"headers_text"}[editor?.dataset.valueEditor];
    if(field){try{state.mcpDraft[field]=json(readEditor(editor));editor.removeAttribute("data-invalid");state.mcpDirty=true;}catch(error){editor.dataset.invalid=error.message;}return;}
    if(e.id==="mcp-search"){state.mcpQuery=e.value;document.querySelector("#mcp-cards").innerHTML=mcpCards();}
    if(e.id==="mcp-directory")state.mcpDirectory=e.value;
    if(e.dataset.mcp&&state.mcpDraft){state.mcpDraft[e.dataset.mcp]=e.type==="checkbox"?e.checked:e.type==="number"?Number(e.value):e.value;state.mcpDirty=true;}
  }
  function change(e){launch.change(e);if(e.dataset.mcp==="transport")render();}
  async function submit(event){
    if(event.target.id==="mcp-registry-form"){await call("register_directory",{directory:state.mcpDirectory.trim()});state.mcpDirectory="";await loadBootstrap();render();toast("MCP 目录已注册");return;}
    if(event.target.id!=="mcp-form"||state.mcpSaving)return;
    const draft=state.mcpDraft;
    if(draft.transport==="stdio")launch.commit(draft);
    const definition={...draft.definition,id:draft.id,name:draft.name,description:draft.description,project_id:state.project.id,enabled:draft.enabled,expose_to_codex:draft.expose_to_codex,session_scope:draft.session_scope,allowed_tools:draft.allowed_text.trim()?draft.allowed_text.trim().split(/\r?\n/).map(value=>value.trim()).filter(Boolean):null,transport:draft.transport,timeout_seconds:draft.timeout_seconds,command:draft.transport==="stdio"?[draft.command,...draft.args]:[],cwd:draft.transport==="stdio"?draft.cwd||null:null,url:draft.transport==="stdio"?"":draft.url,environment:draft.transport==="stdio"?draft.environment||{}:{},environment_refs:draft.transport==="stdio"?draft.environment_refs||{}:{},header_refs:draft.transport==="stdio"?{}:draft.header_refs||{}};
    const field=draft.transport==="stdio"?"mcp-env":"mcp-headers",editor=document.querySelector('[data-value-editor="'+field+'"]');
    const credentials=editor?readEditor(editor):{};
    if(Object.values(credentials).some(value=>typeof value!=="string"))throw new Error("凭据名称和值都需要是文本。");
    state.mcpSaving=true;render();
    try{
      let saved=await call("save",{definition,expected_revision:draft.revision||0});
      for(const [name,value]of Object.entries(credentials)){const receipt=await call("set_credential",{server_id:definition.id,target:draft.transport==="stdio"?"environment":"header",name,value,expected_revision:saved.revision});saved.revision=receipt.revision;}
      if(state.mcpDraft===draft)await select(definition.id);
      await loadBootstrap();toast("MCP 服务已保存，可以开始连接测试");
    }finally{state.mcpSaving=false;render();}
  }
  return {page:mcpPage,action,input:onInput,change,submit,toggle:launch.toggle,view};
}
