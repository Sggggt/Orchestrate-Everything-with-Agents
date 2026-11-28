import { pluginName } from "./library.mjs";
import { listChoices, modelOptions } from "./api-box.mjs";
import { $,escape,icon,iconButton,signal,hint as helpHint,rpc,element,modal,notify,empty,confirmRemoval } from "./ui.mjs";
import { defaults,formField,getAt,setAt,schemaAt } from "./configuration-form.mjs";
import { renderArchitecture } from "./architecture-settings.mjs";
import { reveal } from "./motion.mjs";

const drafts=new Map();let selected=null,tab=0,createRequested=false;

export function selectSettingsAgent(id,create=false){if(id)selected=id;tab=0;createRequested=create;}
export function clearRemovedAgent(app,id){drafts.delete(app.state.project.id+":"+id);if(selected===id)selected=null;}
export async function removeAgent(app,id,revision){
  const result=await app.rpc("agents.remove",{agent_id:id,project_id:app.state.project.id,expected_revision:revision});
  clearRemovedAgent(app,id);await app.reloadAgents();return result;
}
const groups=[
  ["概览与画布","配置、草稿与生效版本。",[]],
  ["基本信息与职责","",[["instructions","system"]]],
  ["常用能力","工具、Skills 与工作环境。",[["tools","local"],["tools","mcp"],["skills"],["workspace","inherit_project"],["workspace","sandbox"],["workspace","network"],["workspace","project"],["workspace","references"],["context","input_tokens"],["execution","budget"]]],
  ["通信与协调","",[]]
];
const basic={workspace:["inherit_project","project","work","references","writable_project_paths","writable_directories","sandbox","network"],budget:["max_uncached_input_tokens","max_output_tokens","max_cost","currency","max_elapsed_seconds"]};

function partnerFields(owner, agents, role, changed) {
  const prefix=`partners-${role}`;
  const section=element(`<div class="partner-fields"><div class="field"><label for="${prefix}">允许联系的伙伴</label><div class="input-actions"><input id="${prefix}" list="${prefix}-options" placeholder="选择伙伴或输入 Agent ID"><datalist id="${prefix}-options"></datalist><button type="button" class="btn btn-small">添加</button></div><div class="partner-list"></div></div><label class="permission-toggle"><input type="checkbox">允许向伙伴派发工作</label></div>`);
  const input=$("input[list]",section),list=$(".partner-list",section),toggle=$("input[type=checkbox]",section);
  const partners=agents.map(row=>row.definition).filter(agent=>agent.id!==owner.id);
  const names=new Map(partners.map(agent=>[agent.id,agent.name]));
  $("datalist",section).innerHTML=partners.map(agent=>`<option value="${escape(agent.id)}" label="${escape(agent.name)}"></option>`).join("");
  const render=()=>{
    list.replaceChildren();
    for(const id of owner.communication?.departments||[]){
      const row=element(`<span class="partner-entry"><span title="${escape(id)}">${escape(names.get(id)||id)}</span>${iconButton("close",`移除 ${names.get(id)||id}`)}</span>`);
      $("button",row).onclick=()=>{owner.communication.departments=owner.communication.departments.filter(item=>item!==id);changed();render();};
      list.append(row);
    }
  };
  const add=()=>{
    const id=input.value.trim();
    if(!id)return;
    if(!/^[a-z][a-z0-9_-]{0,63}$/.test(id)){notify("请输入有效的 Agent ID。",true);return;}
    const permission=owner.communication??={};
    permission.departments=[...new Set([...(permission.departments||[]),id])];
    input.value="";changed();render();
  };
  $(".input-actions button",section).onclick=add;
  input.onkeydown=event=>{if(event.key==="Enter"){event.preventDefault();add();}};
  toggle.checked=!!owner.communication?.can_request;
  toggle.onchange=()=>{(owner.communication??={}).can_request=toggle.checked;changed();};
  render();return section;
}
export function configurationCheck(check){
  return `<section class="v2-panel"><h3>已保存配置的检查 · 修订 ${escape(check.revision)}</h3><dl class="v2-kv"><dt>工具配置</dt><dd>${check.tools.unavailable.length?escape(check.tools.unavailable.join("、")):"配置可用"}</dd><dt>目录检查</dt><dd>${check.workspace_check.state==="resolved"?"目录已解析":escape(check.workspace_check.issues.map(value=>value.code).join("、"))}</dd><dt>程序查找</dt><dd>${check.executables.length?check.executables.map(value=>`${escape(value.kind)}：${value.available?"已找到":"未找到"}`).join("；"):"未配置外部程序"}</dd></dl><details><summary>查看能力与目录</summary><pre class="v2-pre">${escape(JSON.stringify({tools:check.tools,workspace:check.workspace_check,executables:check.executables},null,2))}</pre></details></section>`;
}
function draftFor(app,identity){const key=app.state.project.id+":"+identity,row=app.state.agents.find(value=>value.definition.id===identity),existing=drafts.get(key);if(!existing||row&&!existing.dirty&&existing.revision!==row.revision){drafts.set(key,{definition:structuredClone(row.definition),revision:row.revision,dirty:false});}return drafts.get(key);}
export async function renderSettings(app,root){
  const s=app.state;root.className="settings-layout";
  if(selected&&!s.agents.some(value=>value.definition.id===selected)&&!drafts.has(s.project.id+":"+selected))selected=null;
  selected??=s.agents[0]?.definition.id;
  root.innerHTML=`<aside class="settings-list"><button class="icon-btn" id="new-agent" title="新建 Agent" aria-label="新建 Agent">${icon("plus")}</button><div id="agent-options">${s.agents.map(row=>`<button class="settings-agent ${row.definition.id===selected?"active":""}" data-agent="${escape(row.definition.id)}"><span class="v2-avatar">${escape(row.definition.name.slice(0,1))}</span><span class="settings-agent-name" title="${escape(row.definition.id)}"><strong>${escape(row.definition.name)}</strong></span><span class="dot ${row.definition.enabled?"online":""}" title="${row.definition.enabled?"启用":"停用"}" aria-label="${row.definition.enabled?"启用":"停用"}"></span></button>`).join("")}</div></aside><section class="settings-form"><div class="v2-heading"><div><h1>Agent 设置</h1></div><div class="v2-actions"><button class="icon-btn" id="import-agent" title="导入配置" aria-label="导入配置">${icon("upload")}</button><button class="icon-btn" id="export-agent" title="导出配置" aria-label="导出配置">${icon("download")}</button></div></div><nav class="v2-tabs" aria-label="配置分组">${groups.map(([title],index)=>`<button data-tab="${index}" class="${index===tab?"active":""}">${title}</button>`).join("")}</nav><div id="config-body"></div></section>`;
  for(const button of root.querySelectorAll("[data-agent]"))button.onclick=async()=>{selected=button.dataset.agent;await renderSettings(app,root);reveal($("#config-body",root));};
  for(const button of root.querySelectorAll("[data-tab]"))button.onclick=async()=>{tab=Number(button.dataset.tab);for(const item of root.querySelectorAll("[data-tab]"))item.classList.toggle("active",item===button);await renderBody();reveal($("#config-body",root));};
  $("#new-agent",root).onclick=()=>newAgent(app,root);
  $("#export-agent",root).onclick=()=>{if(!selected)return;const draft=draftFor(app,selected);const url=URL.createObjectURL(new Blob([JSON.stringify(draft.definition,null,2)],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download=draft.definition.id+".json";link.click();URL.revokeObjectURL(url);};
  $("#import-agent",root).onclick=()=>{const content=element(`<form><div class="field"><label for="agent-import">Agent 配置 JSON</label><textarea id="agent-import" rows="14" required></textarea></div><p><button type="submit" class="icon-btn primary" title="载入表单" aria-label="载入表单">${icon("upload")}</button></p></form>`);const close=modal("导入 Agent 配置",content);content.onsubmit=event=>{event.preventDefault();try{const definition=JSON.parse($("textarea",content).value);if([definition,definition.manager].some(owner=>owner&&("model" in owner||"summary_model" in owner)))throw new Error("请通过离线迁移转换旧模型配置，再导入 Box 引用。");if(!definition.id||!definition.name)throw new Error("配置需要 id 和 name");if(definition.project_id&&definition.project_id!==s.project.id)throw new Error("配置属于其他项目，请检查项目归属");const row=s.agents.find(value=>value.definition.id===definition.id);definition.project_id??=s.project.id;drafts.set(s.project.id+":"+definition.id,{definition,revision:row?.revision||0,dirty:true});selected=definition.id;close();renderSettings(app,root);}catch(error){notify(error.message,true);}};};
  async function renderBody(){
    const body=$("#config-body",root);if(!selected){body.innerHTML=empty("添加一个 Agent","");return;}
    const draft=draftFor(app,selected),[title,hint,paths]=groups[tab];
    const definition=draft.definition,tools=definition.tools||{},toolCount=(tools.local?.length||0)+(tools.functions?.length||0);
    const form=element(`<form class="v2-config-form">
      <header class="settings-profile"><span class="settings-profile-avatar" title="${escape(definition.id)}">${icon("agents")}</span><div><h2>${escape(definition.name||selected)}</h2><div class="settings-signals"><span data-availability>${signal(definition.enabled?"pending":"muted",definition.enabled?"配置待检查":"已停用")}<span class="signal-label">${definition.enabled?"配置待检查":"已停用"}</span></span></div></div>${iconButton("check","保存设置",{type:"submit",className:"primary"})}</header>
      <div class="settings-capabilities"><span title="本地工具" aria-label="${toolCount} 个本地工具">${icon("terminal")}${toolCount}</span>${tools.mcp?.length?`<span title="MCP 服务" aria-label="${tools.mcp.length} 个 MCP 服务">${icon("plug")}${tools.mcp.length}</span>`:""}<span title="Skills" aria-label="${definition.skills?.length||0} 个 Skill">${icon("book")}${definition.skills?.length||0}</span><span title="${definition.workspace?.inherit_project!==false?"继承项目环境":"独立工作环境"}" aria-label="${definition.workspace?.inherit_project!==false?"继承项目环境":"独立工作环境"}">${icon("folder")}</span></div>
      <div id="architecture-settings" ${tab!==0?'hidden':""}></div><div id="config-fields" class="settings-fields"></div><div id="config-check" role="status"></div>
      <div class="settings-extra settings-management"><span class="v2-muted">${escape(definition.id)}</span></div>
      <footer class="v2-savebar"><span id="save-status" class="v2-muted"></span><div class="v2-actions">${iconButton("shield","检查配置",{id:"check-agent"})}</div></footer></form>`);body.replaceChildren(form);
    const changed=()=>{draft.dirty=true;$("#save-status",form).innerHTML=signal("pending","有未保存的修改");};
    const fields=$("#config-fields",form);
    const add=(path,schema,target=fields)=>{const name=path.at(-1);target.append(formField({schema:schema||schemaAt(s.schema,path),root:s.schema,get:()=>getAt(draft.definition,path),set:value=>setAt(draft.definition,path,value),name,path,changed,basic:basic[name]}));};
    if(tab===1){const metadata=element('<section class="schema-object"><h3>基本信息</h3><div class="form-grid"></div></section>');$("#config-fields",form).append(metadata);for(const key of ["name","description","enabled"]){const field=formField({schema:s.schema.properties[key],root:s.schema,get:()=>draft.definition[key],set:value=>draft.definition[key]=value,name:key,path:[key],changed});$(".form-grid",metadata).append(field);}}
    if(tab===2)await pluginFields(app,definition,fields,changed);
    for(const path of paths)add(path);
    if(tab===3){
      const section=element(`<section class="communication-settings"><fieldset><legend>主 Agent</legend><div data-main-partners></div></fieldset><fieldset><legend>manager</legend><label class="permission-toggle"><input type="checkbox" id="enable-manager">配置协调角色</label><div data-manager-fields></div></fieldset></section>`);
      fields.append(section);
      $("[data-main-partners]",section).append(partnerFields(definition,s.agents,"main",changed));
      const managerFields=$("[data-manager-fields]",section),toggle=$("#enable-manager",section);
      let previousManager=definition.manager;
      const models=await listChoices(app,"models.list");
      const renderManager=()=>{
        managerFields.replaceChildren();
        if(!definition.manager)return;
        const model=element(`<label class="field">manager 模型<select>${modelOptions(models,definition.manager.model_ref)}</select></label>`);
        $("select",model).onchange=event=>{definition.manager.model_ref=event.target.value||null;changed();};
        managerFields.append(model);
        add(["manager","instructions","system"],null,managerFields);
        managerFields.append(partnerFields(definition.manager,s.agents.filter(row=>row.definition.id!==definition.id),"manager",changed));
      };
      toggle.checked=!!definition.manager;
      toggle.onchange=()=>{
        if(toggle.checked)definition.manager=previousManager??defaults(s.schema.$defs.Manager,s.schema);
        else {previousManager=definition.manager;definition.manager=null;}
        changed();renderManager();
      };
      renderManager();
    }
    $("#save-status",form).innerHTML=draft.dirty?signal("pending","有未保存的修改"):`${signal("online","配置已保存")}<span>配置 r${draft.revision}</span>`;
    const availability=check=>{if(!form.isConnected)return;const enabled=definition.enabled,ready=(!!definition.execution.program?.length&&!definition.execution.program_uses_model||check.model.state==="configured")&&!check.tools.unavailable.length&&check.workspace_check.state==="resolved";const label=!enabled?"已停用":ready?"配置可用":"需要配置";$("[data-availability]",form).innerHTML=signal(!enabled?"muted":ready?"online":"pending",label)+`<span class="signal-label" title="配置检查不代表模型连接已验证">${label}</span>`;};
    if(draft.revision)rpc("agents.check",{agent_id:definition.id,project_id:s.project.id}).then(availability).catch(error=>{if(form.isConnected)$("[data-availability]",form).textContent="检查失败："+error.message;});
    $("#check-agent",form).disabled=draft.revision===0;
    $("#check-agent",form).onclick=async()=>{try{const check=await rpc("agents.check",{agent_id:selected,project_id:s.project.id});$("#config-check",form).innerHTML=configurationCheck(check);availability(check);}catch(error){notify(error.message,true);}};
    const remove=element(iconButton("trash","删除 Agent",{className:"danger-text"}));
    remove.disabled=draft.revision===0;$(".settings-extra",form).append(remove);
    remove.onclick=()=>{const id=draft.definition.id;confirmRemoval("删除 Agent",`删除 ${draft.definition.name} 后将停止接受新工作。历史任务、回执、产物和源文件保留；未结束或待核对的工作会阻止删除。`,async()=>{await removeAgent(app,id,draft.revision);await renderSettings(app,root);notify("Agent 已删除，历史和文件已保留。");});};
    form.onsubmit=async event=>{
      event.preventDefault();const button=form.querySelector("button[type=submit]");button.disabled=true;
      try{
        const saved=await rpc("agents.save",{definition:draft.definition,expected_revision:draft.revision});
        Object.assign(draft,{revision:saved.revision,definition:saved.definition,dirty:false});
        await app.reloadAgents();notify("配置已保存");await renderSettings(app,root);
      }catch(error){notify(error.message,true);}finally{button.disabled=false;}
    };
    if(tab===0)await renderArchitecture(app,$("#architecture-settings",form),draft);
  }
  await renderBody();
  if(!root.isConnected)return;
  if(createRequested){createRequested=false;await newAgent(app,root);}
  app.onAgentEvent=fact=>{if(fact?.kind==="agent.removed")clearRemovedAgent(app,fact.data.agent_id);if(s.page!=="agent-settings"||!root.isConnected)return;const current=selected&&draftFor(app,selected);if(current?.dirty){const status=$("#save-status",root);if(status)status.textContent="服务端已更新，请核对当前草稿";}else renderSettings(app,root);};
  app.onArchitectureEvent=fact=>{if(s.page!=="agent-settings"||!root.isConnected||fact.project_id&&fact.project_id!==s.project.id||tab!==0)return;const area=$("#architecture-settings",root),draft=selected&&draftFor(app,selected);if(area&&draft)renderArchitecture(app,area,draft);};
}
async function newAgent(app,root){
  const projectId=app.state.project.id;
  let models;
  try{models=await listChoices(app,"models.list");}catch(error){notify(error.message,true);return;}
  if(!root.isConnected||app.state.page!=="agent-settings"||app.state.project?.id!==projectId)return;
  const form=element(`<form><label class="field">Agent 名称<input name="agentName" required maxlength="80"></label><label class="field">稳定标识<input name="identity" required pattern="[a-z][a-z0-9_\\-]{0,63}" placeholder="reviewer"></label>
    <label class="field">默认模型<select name="modelRef" required>${modelOptions(models,null)}</select></label>${models.length?"":'<p>API Box 中尚无可选模型。</p><button type="button" class="btn" data-box>打开 API Box</button>'}
    <footer class="dialog-footer"><button class="btn primary" ${models.length?"":"disabled"}>创建配置草稿</button></footer></form>`);
  const close=modal("新建 Agent",form);
  if($("[data-box]",form))$("[data-box]",form).onclick=()=>{close();app.navigate("api-box");};
  form.onsubmit=event=>{
    event.preventDefault();const fields=form.elements,id=fields.identity.value;
    if(app.state.agents.some(row=>row.definition.id===id)){notify("此标识已存在。",true);return;}
    const definition=defaults(app.state.schema,app.state.schema);
    Object.assign(definition,{id,name:fields.agentName.value,model_ref:fields.modelRef.value,project_id:projectId});
    drafts.set(projectId+":"+id,{definition,revision:0,dirty:true});selected=id;tab=0;close();renderSettings(app,root);
  };
}

async function pluginFields(app,definition,fields,changed){
  const project_id=app.state.project.id;
  const plugins=(await listChoices(app,"plugins.list",{project_id})).map(item=>({...item,name:pluginName(item)}));
  const section=element('<section class="schema-object plugin-grants"><h3>插件</h3></section>');
  fields.append(section);
  const creation=definition.creation??={enabled:false,model_refs:[],plugins:[],recursive:false};
  const options=element('<div class="grant-grid"></div>');section.append(options);
  function choices(parent,items,selected,update){
    for(const item of items){
      const label=element(`<label class="permission-toggle"><input type="checkbox" ${selected.includes(item.id)?"checked":""}>${escape(item.name)}${item.enabled===false?" · 已停用":""}</label>`);
      $("input",label).onchange=event=>{update(item.id,event.target.checked);changed();};
      parent.append(label);
    }
  }
  const toggle=(items,id,enabled)=>enabled?[...new Set([...items,id])]:items.filter(value=>value!==id);
  const factory=element('<fieldset><legend>Agent 工厂</legend><label class="permission-toggle"><input type="checkbox" data-enable>允许创建 Agent</label><div data-creation></div></fieldset>');
  choices(options,plugins,definition.plugins||[],(id,on)=>{definition.plugins=toggle(definition.plugins||[],id,on);if(id==="agent-factory"&&!on)creation.enabled=false;sync();});
  section.append(factory);
  const enable=$("[data-enable]",factory),permissions=$("[data-creation]",factory);
  function sync(){
    const bound=definition.plugins?.includes("agent-factory");
    factory.hidden=!bound;enable.disabled=!bound;enable.checked=creation.enabled;
    permissions.hidden=!creation.enabled;

  }
  enable.onchange=()=>{creation.enabled=enable.checked;changed();sync();};
  sync();
  const models=await listChoices(app,"models.list");
  permissions.innerHTML='<h4>允许的模型</h4><div data-models></div><h4>允许的插件</h4><div data-plugins></div><label class="permission-toggle"><input type="checkbox" data-recursive>允许新 Agent 继续创建定义</label>';
  choices($("[data-models]",permissions),models,creation.model_refs,(id,on)=>{creation.model_refs=toggle(creation.model_refs,id,on);});
  choices($("[data-plugins]",permissions),plugins,creation.plugins,(id,on)=>{creation.plugins=toggle(creation.plugins,id,on);});
  const recursive=$("[data-recursive]",permissions);recursive.checked=creation.recursive;
  recursive.onchange=()=>{creation.recursive=recursive.checked;changed();};
}
