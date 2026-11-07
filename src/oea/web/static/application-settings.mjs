import { $,escape,icon,element,modal,notify,confirmRemoval } from "./ui.mjs";
import { defaults,formField,labels } from "./configuration-form.mjs";

Object.assign(labels,{grant:"应用授权",trusted_extensions:"允许受信任的代码扩展",application_agent_md:"允许读取 Agent 运行指令",application_model:"允许模型调用"});

function showCredential(value){
  const content=element(`<div><p>请保存到应用后端的秘密配置。凭据只在本次创建或轮换时显示。</p><div class="field"><label for="application-credential">应用凭据</label><input id="application-credential" readonly autocomplete="off" spellcheck="false"></div><button class="btn" data-copy>复制凭据</button></div>`);
  const input=$("input",content);input.value=value.credential;
  $("[data-copy]",content).onclick=async()=>{try{await navigator.clipboard.writeText(input.value);notify("凭据已复制。");}catch{input.select();notify("请选择并复制凭据。");}};
  const close=modal("应用凭据",content);close.onclose=()=>{input.value="";};
}

export async function manageApplications(app,root){
  let selected=null,applications=[],cursor=null,bindings=[],bindingCursor=null;
  const active=()=>root.isConnected&&app.state.page==="applications";
  const perform=async(action)=>{try{await action();}catch(error){notify(error.message,true);}};
  const load=async(more=false)=>{
    const page=await app.rpc("applications.list",{cursor:more?cursor:0,limit:25});
    applications=more?[...applications,...page.items]:page.items;cursor=page.cursor;
    selected=applications.find(item=>item.id===selected?.id)||applications[0]||null;
    if(selected)await loadBindings();else{bindings=[];bindingCursor=null;}
    draw();
  };
  const loadBindings=async(more=false)=>{
    const page=await app.rpc("applications.bindings",{app_id:selected.id,cursor:more?bindingCursor:0,limit:25});
    bindings=more?[...bindings,...page.items]:page.items;bindingCursor=page.cursor;
  };
  const status=item=>item.removed?"已删除":item.enabled?"已启用":"已停用";
  function draw(){
    if(!active())return;
    root.className="applications-page";
    root.innerHTML=`<div class="page-heading"><div><h1>应用接入</h1><p class="form-hint">向本机应用授予 Agent。应用会话和成果由所属应用读取。</p></div><button class="btn primary" data-new>${icon("plus")}登记应用</button></div><div class="application-layout"><aside class="application-list" aria-label="应用列表">${applications.map(item=>`<button data-select="${escape(item.id)}" class="settings-agent ${selected?.id===item.id?"active":""}"><span>${icon("plug")}</span><span class="settings-agent-name"><strong>${escape(item.name)}</strong><small>${status(item)}</small></span></button>`).join("")||'<p class="quiet-state">尚未登记应用。</p>'}${cursor!=null?'<button class="btn" data-more>加载更多应用</button>':""}</aside><section class="application-detail">${selected?`<div class="v2-panel"><div class="section-heading"><div><h2>${escape(selected.name)}</h2><p class="form-hint">${status(selected)} · 修订 ${selected.revision}</p></div><div class="v2-actions"><button class="btn" data-toggle ${selected.removed?"disabled":""}>${selected.enabled?"停用":"启用"}</button><button class="btn" data-rotate ${selected.removed?"disabled":""}>轮换凭据</button><button class="icon-btn danger-text" data-remove ${selected.removed?"disabled":""} aria-label="删除应用">${icon("trash")}</button></div></div><dl class="v2-kv"><dt>应用 ID</dt><dd>${escape(selected.id)}</dd><dt>接口</dt><dd>OAP 1.0 · /api/oap/v1</dd></dl></div><div class="section-heading"><h2>Agent 授权</h2><button class="btn" data-bind ${selected.removed?"disabled":""}>添加授权</button></div><div class="application-bindings">${bindings.map(item=>`<article class="v2-panel"><div class="section-heading"><div><h3>${escape(app.state.agents.find(row=>row.definition.id===item.agent_id)?.definition.name||item.agent_id)}</h3><p class="form-hint">${item.enabled?"已启用":"已撤销"} · ${escape(item.project_id===app.state.project.id?app.state.project.name:item.project_id)}</p></div><button class="btn" data-edit="${escape(item.id)}" ${selected.removed?"disabled":""}>调整授权</button></div><p class="form-hint">模型：${item.grant.model?"允许":"禁止"} · 本地工具：${item.grant.tools.local.length} · MCP：${item.grant.tools.mcp.length} · Skills：${item.grant.skills.length}</p><p class="form-hint">网络：${item.grant.workspace.network==="allow"?"允许":"禁止"} · 单次工作费用上限：${item.grant.budget.max_cost==null?"未单独设置":escape(item.grant.budget.max_cost)+" "+escape(item.grant.budget.currency)}</p></article>`).join("")||'<p class="quiet-state">添加授权后，应用才能创建 Agent 会话。</p>'}</div>${bindingCursor!=null?'<button class="btn" data-more-bindings>加载更多授权</button>':""}`:'<div class="v2-panel"><h2>连接你的第一个应用</h2><p>登记应用并保存凭据，再选择当前项目的 Agent 和获准能力。</p></div>'}</section></div>`;
    $("[data-new]",root).onclick=()=>create();
    root.querySelectorAll("[data-select]").forEach(button=>button.onclick=()=>perform(async()=>{selected=applications.find(item=>item.id===button.dataset.select);await loadBindings();draw();}));
    if(cursor!=null)$("[data-more]",root).onclick=()=>perform(()=>load(true));
    if(!selected)return;
    $("[data-toggle]",root).onclick=()=>perform(async()=>{await app.rpc("applications.set_enabled",{app_id:selected.id,enabled:!selected.enabled,expected_revision:selected.revision});await load();});
    $("[data-remove]",root).onclick=()=>{const item=selected;confirmRemoval("删除应用",`删除 ${item.name} 后将撤销接入，原有历史和文件保留。`,async()=>{await app.rpc("applications.remove",{app_id:item.id,expected_revision:item.revision});await load();});};
    $("[data-rotate]",root).onclick=()=>rotate(selected);
    $("[data-bind]",root).onclick=()=>perform(()=>editBinding());
    root.querySelectorAll("[data-edit]").forEach(button=>button.onclick=()=>perform(()=>editBinding(bindings.find(item=>item.id===button.dataset.edit))));
    if(bindingCursor!=null)$("[data-more-bindings]",root).onclick=()=>perform(async()=>{await loadBindings(true);draw();});
  }
  function create(){
    const content=element('<form><div class="field"><label for="application-name">应用名称</label><input id="application-name" required maxlength="80" autocomplete="off"></div><button class="btn primary" type="submit">登记并生成凭据</button></form>');
    const close=modal("登记应用",content);
    content.onsubmit=event=>{event.preventDefault();perform(async()=>{const button=$("button",content);button.disabled=true;try{const value=await app.rpc("applications.create",{name:$("input",content).value});selected=value.application;await close();await load();showCredential(value);}finally{button.disabled=false;}});};
  }
  function rotate(item){
    const content=element('<div><p>生成后旧凭据立即失效。应用身份、会话和原有成果保留。</p><button class="btn primary">生成新凭据</button></div>');
    const close=modal("轮换应用凭据",content);
    $("button",content).onclick=()=>perform(async()=>{const value=await app.rpc("applications.rotate_credential",{app_id:item.id,expected_revision:item.revision});await close();await load();showCredential(value);});
  }
  async function editBinding(saved){
    const item=selected;
    const guide=await app.rpc("host.guide",{area:"applications",action:"bind"});
    const schema=guide.parameters_schema;
    const grantSchema=schema.$defs.ApplicationGrant;
    const grant=saved?structuredClone(saved.grant):defaults(grantSchema,schema);
    if(!saved){grant.workspace.inherit_project=false;grant.workspace.project=null;grant.workspace.work=null;grant.workspace.sandbox="native";grant.workspace.network="deny";grant.workspace.writable_project_paths=[];}
    const definition=saved?{app_id:item.id,project_id:saved.project_id,agent_id:saved.agent_id,enabled:!!saved.enabled,grant}:{app_id:item.id,project_id:app.state.project.id,agent_id:app.state.agents[0]?.definition.id||"",enabled:true,grant};
    const options=app.state.agents.map(row=>row.definition);
    if(saved&&!options.some(agent=>agent.id===saved.agent_id))options.push({id:saved.agent_id,name:saved.agent_id});
    const content=element(`<form><p class="form-hint">实际能力还受来源 Agent 当前配置约束。目录、网络和受信任扩展按本授权提供。</p><div class="field"><label for="binding-agent">Agent</label><select id="binding-agent" ${saved?"disabled":""}>${options.map(agent=>`<option value="${escape(agent.id)}" ${agent.id===definition.agent_id?"selected":""}>${escape(agent.name)}</option>`).join("")}</select></div><label class="field"><span><input type="checkbox" id="binding-enabled" ${definition.enabled?"checked":""}> 启用此授权</span></label><div data-grant></div><button class="btn primary" type="submit">保存授权</button></form>`);
    const container=$("[data-grant]",content);
    for(const name of ["model","tools","skills","agent_md","workspace","budget","trusted_extensions"]){
      const field=grantSchema.properties[name];
      let visible=field;
      if(name==="workspace")visible={...schema.$defs.Workspace,properties:Object.fromEntries(Object.entries(schema.$defs.Workspace.properties).filter(([key])=>!["project","work","inherit_project","sandbox","writable_project_paths"].includes(key)))};
      const basic={tools:["local","mcp"],workspace:["references","writable_directories","network"],budget:["max_cost","currency","max_total_input_tokens","max_output_tokens","max_elapsed_seconds"]}[name];
      container.append(formField({schema:visible,root:schema,name:name==="agent_md"?"application_agent_md":name==="model"?"application_model":name,path:[name],basic,get:()=>definition.grant[name],set:value=>{definition.grant[name]=value;}}));
    }
    const close=modal(saved?"调整 Agent 授权":"添加 Agent 授权",content,{size:"wide"});
    content.onsubmit=event=>{event.preventDefault();perform(async()=>{definition.agent_id=$("#binding-agent",content).value;definition.enabled=$("#binding-enabled",content).checked;const button=$('button[type="submit"]',content);button.disabled=true;try{await app.rpc("applications.bind",{binding:definition,...(saved?{binding_id:saved.id,expected_revision:saved.revision}:{})});await close();await loadBindings();draw();notify("应用授权已保存。");}finally{button.disabled=false;}});};
  }
  await load();
}
