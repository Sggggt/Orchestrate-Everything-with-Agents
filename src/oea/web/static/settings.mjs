import { $,escape,icon,rpc,element,modal,notify,empty,confirmRemoval } from "./ui.mjs";
import { defaults,formField,getAt,setAt,schemaAt } from "./configuration-form.mjs";

const drafts=new Map();let selected=null,tab=0,createRequested=false;
const probes=new Map();
export function selectSettingsAgent(id,create=false){if(id)selected=id;tab=0;createRequested=create;}
export function clearRemovedAgent(app,id){drafts.delete(app.state.project.id+":"+id);if(selected===id)selected=null;}
export async function removeAgent(app,id,revision){
  const result=await app.rpc("agents.remove",{agent_id:id,project_id:app.state.project.id,expected_revision:revision});
  clearRemovedAgent(app,id);await app.reloadAgents();return result;
}
const groups=[
  ["职责与模型","定义清楚的职责，选择实际使用的模型。",[["model"],["instructions"]]],
  ["工具与 Skills","业务工具参数默认按需加载；Skill 先提供目录，必需正文在执行前加载。",[["tools"],["skills"]]],
  ["上下文与记忆","保留原文引用，控制模型的活跃输入与阶段记忆。",[["context"]]],
  ["工作目录与环境","项目原件与实例工作目录分开，恢复时继续使用原实例目录。",[["workspace"]]],
  ["执行与交付","选择执行图，设置交付要求与累计费用、时间预算。",[["execution"]]],
  ["通信与 manager","明确伙伴范围，单独配置manager的模型与协调指令。",[["communication"],["manager"]]]
];
const basic={model:["base_url","model","credential_ref"],workspace:["project","work","references","writable_project_paths"],execution:["graph","graph_version","delivery","budget"],manager:["model","instructions","communication"]};
export function configurationCheck(check){
  const labels={base_url:"API 地址",model:"模型名称",credential:"密钥来源"};
  const model=value=>value.state==="configured"?`参数已配置 · ${value.has_credential?"密钥引用可用":"未绑定密钥引用"}`:"缺少："+escape(value.missing.map(key=>labels[key]||key).join("、"));
  return `<section class="v2-panel"><h3>已保存配置的检查 · 修订 ${escape(check.revision)}</h3><dl class="v2-kv"><dt>主模型参数</dt><dd>${model(check.model)}</dd><dt>manager 模型参数</dt><dd>${check.manager?model(check.manager.model):"未配置，可使用 direct 模式"}</dd><dt>模型连接</dt><dd>尚未发起真实连接测试</dd><dt>工具配置</dt><dd>${check.tools.unavailable.length?escape(check.tools.unavailable.join("、")):"当前绑定与宿主授权可用"}</dd><dt>目录检查</dt><dd>${check.workspace_check.state==="resolved"?"新实例路径已解析；未创建目录":escape(check.workspace_check.issues.map(value=>value.code).join("、"))}</dd><dt>程序查找</dt><dd>${check.executables.length?check.executables.map(value=>`${escape(value.kind)}：${value.available?"已找到":"未找到"}`).join("；"):"未配置外部程序"}</dd><dt>真实任务</dt><dd>请在运行记录中核对实际交付</dd></dl><details><summary>查看能力与目录</summary><pre class="v2-pre">${escape(JSON.stringify(check,null,2))}</pre></details></section>`;
}
function draftFor(app,identity){const key=app.state.project.id+":"+identity,row=app.state.agents.find(value=>value.definition.id===identity),existing=drafts.get(key);if(!existing||row&&!existing.dirty&&existing.revision!==row.revision){drafts.set(key,{definition:structuredClone(row.definition),revision:row.revision,dirty:false,secrets:{}});}return drafts.get(key);}
export async function renderSettings(app,root){
  const s=app.state;root.className="settings-layout";
  if(selected&&!s.agents.some(value=>value.definition.id===selected)&&!drafts.has(s.project.id+":"+selected))selected=null;
  selected??=s.agents[0]?.definition.id;
  root.innerHTML=`<aside class="settings-list"><button class="icon-btn" id="new-agent" title="新建 Agent" aria-label="新建 Agent">${icon("plus")}</button><div id="agent-options">${s.agents.map(row=>`<button class="settings-agent ${row.definition.id===selected?"active":""}" data-agent="${escape(row.definition.id)}"><span class="v2-avatar">${escape(row.definition.name.slice(0,1))}</span><span class="settings-agent-name" title="${escape(row.definition.id)}"><strong>${escape(row.definition.name)}</strong></span><span class="dot ${row.definition.enabled?"online":""}" title="${row.definition.enabled?"启用":"停用"}" aria-label="${row.definition.enabled?"启用":"停用"}"></span></button>`).join("")}</div></aside><section class="settings-form"><div class="v2-heading"><div><h1>Agent 设置</h1></div><div class="v2-actions"><button class="icon-btn" id="import-agent" title="导入配置" aria-label="导入配置">${icon("upload")}</button><button class="icon-btn" id="export-agent" title="导出配置" aria-label="导出配置">${icon("download")}</button></div></div><nav class="v2-tabs" aria-label="配置分组">${groups.map(([title],index)=>`<button data-tab="${index}" class="${index===tab?"active":""}">${title}</button>`).join("")}</nav><div id="config-body"></div></section>`;
  for(const button of root.querySelectorAll("[data-agent]"))button.onclick=()=>{selected=button.dataset.agent;renderSettings(app,root);};
  for(const button of root.querySelectorAll("[data-tab]"))button.onclick=()=>{tab=Number(button.dataset.tab);for(const item of root.querySelectorAll("[data-tab]"))item.classList.toggle("active",item===button);renderBody();};
  $("#new-agent",root).onclick=()=>newAgent(app,root);
  $("#export-agent",root).onclick=()=>{if(!selected)return;const draft=draftFor(app,selected);const url=URL.createObjectURL(new Blob([JSON.stringify(draft.definition,null,2)],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download=draft.definition.id+".json";link.click();URL.revokeObjectURL(url);};
  $("#import-agent",root).onclick=()=>{const content=element('<form><p class="v2-muted">粘贴完整配置，检查六个分组后再保存。秘密使用引用。</p><div class="field"><label for="agent-import">Agent 配置 JSON</label><textarea id="agent-import" rows="14" required></textarea></div><p><button class="btn primary">载入表单</button></p></form>');const close=modal("导入 Agent 配置",content);content.onsubmit=event=>{event.preventDefault();try{const definition=JSON.parse($("textarea",content).value);if(!definition.id||!definition.name)throw new Error("配置需要 id 和 name");if(definition.project_id&&definition.project_id!==s.project.id)throw new Error("配置属于其他项目，请检查项目归属");const row=s.agents.find(value=>value.definition.id===definition.id);definition.project_id??=s.project.id;drafts.set(s.project.id+":"+definition.id,{definition,revision:row?.revision||0,dirty:true,secrets:{}});selected=definition.id;close();renderSettings(app,root);}catch(error){notify(error.message,true);}};};
  function renderBody(){
    const body=$("#config-body",root);if(!selected){body.innerHTML=empty("添加一个 Agent","先定义职责与模型，再按需要绑定工具、Skill 和工作目录。");return;}
    const draft=draftFor(app,selected),[title,hint,paths]=groups[tab];
    const form=element(`<form class="v2-config-form"><h2>${escape(draft.definition.name||selected)}</h2><div id="config-fields"></div><div id="credential-fields"></div><div id="config-check"></div><footer class="v2-savebar"><span id="save-status" class="v2-muted"></span><div class="v2-actions"><button type="button" class="icon-btn" id="check-agent" title="检查已保存配置" aria-label="检查已保存配置">${icon("shield")}</button><button type="submit" class="icon-btn" title="保存配置" aria-label="保存配置">${icon("check")}</button></div></footer></form>`);body.replaceChildren(form);
    const changed=()=>{draft.dirty=true;$("#save-status",form).textContent="有未保存的修改";for(const input of form.querySelectorAll("[data-secret-role=manager]"))input.disabled=draft.definition.manager==null;};
    const add=(path,schema)=>{const name=path.at(-1);$("#config-fields",form).append(formField({schema:schema||schemaAt(s.schema,path),root:s.schema,get:()=>getAt(draft.definition,path),set:value=>setAt(draft.definition,path,value),name,path,changed,basic:basic[name]}));};
    if(tab===0){const metadata=element('<section class="schema-object"><h3>基本信息</h3><div class="form-grid"></div></section>');$("#config-fields",form).append(metadata);for(const key of ["name","description","enabled"]){const field=formField({schema:s.schema.properties[key],root:s.schema,get:()=>draft.definition[key],set:value=>draft.definition[key]=value,name:key,path:[key],changed});$(".form-grid",metadata).append(field);}const id=document.createElement("p");id.className="v2-muted";id.textContent="Agent 标识："+draft.definition.id;metadata.append(id);}
    for(const path of paths)add(path);
    if(tab===0||tab===5){const role=tab===0?"main":"manager",model=role==="main"?draft.definition.model:draft.definition.manager?.model;
      const secret=element(`<section class="v2-secret"><label for="model-secret">${role==="main"?"主模型":"manager 模型"}密钥</label><input id="model-secret" type="password" autocomplete="new-password" data-secret-role="${role}" placeholder="填写新密钥以更新；留空保留已绑定来源"><small>${model?.credential_ref?"已绑定秘密引用，密钥值不回显。":"尚未绑定密钥；如供应商不需要认证，可留空。"}</small><button class="btn" type="button" id="detach-secret">解除当前密钥绑定</button></section>`);
      $("input",secret).value=draft.secrets[role]||"";$("input",secret).disabled=role==="manager"&&draft.definition.manager==null;
      $("input",secret).oninput=event=>{draft.secrets[role]=event.target.value;changed();};$("#detach-secret",secret).onclick=()=>{const target=role==="main"?draft.definition.model:draft.definition.manager?.model;if(target){target.credential_ref=null;draft.secrets[role]="";changed();renderBody();}};$("#credential-fields",form).append(secret);
    }
    $("#save-status",form).textContent=draft.dirty?"有未保存的修改":"已保存 · 修订 "+draft.revision;
    $("#check-agent",form).disabled=draft.revision===0;
    $("#check-agent",form).onclick=async()=>{try{const check=await rpc("agents.check",{agent_id:selected,project_id:s.project.id});$("#config-check",form).innerHTML=configurationCheck(check);}catch(error){notify(error.message,true);}};
    const remove=element(`<button type="button" class="icon-btn danger-text" title="删除 Agent" aria-label="删除 Agent">${icon("trash")}</button>`);
    remove.disabled=draft.revision===0;$(".v2-actions",form).prepend(remove);
    remove.onclick=()=>{const id=draft.definition.id;confirmRemoval("删除 Agent",`删除 ${draft.definition.name} 后将停止接受新工作。历史任务、回执、产物和源文件保留；未结束或待核对的工作会阻止删除。`,async()=>{await removeAgent(app,id,draft.revision);await renderSettings(app,root);notify("Agent 已删除，历史和文件已保留。");});};
    if(tab===0||tab===5){
      const role=tab===0?"main":"manager",key=s.project.id+":"+selected+":"+role;
      const button=element('<button type="button" class="btn small">测试模型连接</button>');
      button.disabled=draft.revision===0||!s.session||(role==="manager"&&!draft.definition.manager);
      $(".v2-actions",form).prepend(button);
      button.onclick=async()=>{button.disabled=true;let probe=probes.get(key);if(!probe){probe={idempotency_key:crypto.randomUUID(),agent_id:selected,session_id:s.session.id,role};probes.set(key,probe);}try{
        probe.handle=probe.handle||await rpc("agents.probe",{agent_id:probe.agent_id,session_id:probe.session_id,role:probe.role,idempotency_key:probe.idempotency_key});
        $("#config-check",form).textContent="模型探测已提交："+probe.handle.id;
        const result=await rpc("runs.result",{run_id:probe.handle.id,timeout:30});
        const usage=result.usage.provider_reported;
        $("#config-check",form).textContent=`模型连接已响应 · 输入 ${usage.input_tokens??"未知"} token · 输出 ${usage.output_tokens??"未知"} token · ${probe.handle.id}`;
        probes.delete(key);
      }catch(error){$("#config-check",form).textContent=error.message+(probe.handle?" · "+probe.handle.id:"");if(error.run_id&&error.code!=="RESULT_PENDING")probes.delete(key);}finally{button.disabled=false;button.textContent=probes.has(key)?"查看探测结果":"测试模型连接";}};
    }
    form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector("button[type=submit]");button.disabled=true;try{let saved=await rpc("agents.save",{definition:draft.definition,expected_revision:draft.revision});draft.revision=saved.revision;draft.definition=saved.definition;for(const [role,value]of Object.entries(draft.secrets)){if(value&&(role!=="manager"||draft.definition.manager)){const credential=await rpc("agents.set_credential",{agent_id:selected,project_id:s.project.id,role,value,expected_revision:draft.revision});draft.revision=credential.revision;const model=role==="main"?draft.definition.model:draft.definition.manager.model;model.credential_ref=credential.credential_ref;}}draft.secrets={};draft.dirty=false;await app.reloadAgents();notify("配置已保存，新工作将使用保存的能力。已撤销的权限立即生效。");await renderSettings(app,root);}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  }
  renderBody();
  if(createRequested){createRequested=false;newAgent(app,root);}
  app.onAgentEvent=fact=>{if(fact?.kind==="agent.removed")clearRemovedAgent(app,fact.data.agent_id);if(s.page!=="agent-settings"||!root.isConnected)return;const current=selected&&draftFor(app,selected);if(current?.dirty){const status=$("#save-status",root);if(status)status.textContent="服务端配置已更新；当前草稿保留，保存时将核对修订。";}else renderSettings(app,root);};
}
function newAgent(app,root){const form=element('<form><div class="field"><label for="agent-name">Agent 名称</label><input id="agent-name" required maxlength="80"></div><div class="field"><label for="agent-id">稳定标识</label><input id="agent-id" required pattern="[a-z][a-z0-9_-]{0,63}" placeholder="例如 reviewer"><small>使用小写字母、数字、下划线或连字符。</small></div><p><button class="btn primary">创建配置草稿</button></p></form>');const close=modal("新建 Agent",form);form.onsubmit=event=>{event.preventDefault();const identity=$("#agent-id",form).value;if(app.state.agents.some(row=>row.definition.id===identity)){notify("此标识已存在，请选择已有 Agent 或使用新标识。",true);return;}const definition=defaults(app.state.schema,app.state.schema);Object.assign(definition,{id:identity,name:$("#agent-name",form).value,project_id:app.state.project.id});drafts.set(app.state.project.id+":"+identity,{definition,revision:0,dirty:true,secrets:{}});selected=identity;tab=0;close();renderSettings(app,root);};}
