import { $,escape,icon,rpc,element,modal,notify,badge,date,confirmRemoval } from "./ui.mjs";
import { removeAgent } from "./settings.mjs";
import { renderArtifactsPage,previewArtifact,uploadArtifact } from "./artifact-directory.mjs";
export { renderArtifactsPage };
import { avatar,agentCards,unreadBadge } from "./workbench-shell.mjs";
import { showRun } from "./runs.mjs";
import { markdown } from "./markdown.mjs";

export { renderConversation,disposeConversation } from "./conversation.mjs";
export function bindWorkbench(app,root){
  const toggleNavigation=open=>{app.state.mobileNavigationOpen=open;$(".layout")?.classList.toggle("navigation-open",open);$(".mobile-nav-toggle")?.setAttribute("aria-expanded",String(open));if(open)$(".sidebar-brand")?.focus();else $(".mobile-nav-toggle")?.focus();};
  if($(".mobile-nav-toggle",root))$(".mobile-nav-toggle",root).onclick=()=>toggleNavigation(!app.state.mobileNavigationOpen);
  if($(".nav-scrim",root))$(".nav-scrim",root).onclick=()=>toggleNavigation(false);
  for(const button of root.querySelectorAll("[data-page]"))button.onclick=()=>app.navigate(button.dataset.page);
  for(const button of root.querySelectorAll("[data-configure-agent]"))button.onclick=()=>app.openSettings(button.dataset.configureAgent);
  for(const button of root.querySelectorAll("[data-remove-agent]"))button.onclick=()=>{const row=app.state.agents.find(value=>value.definition.id===button.dataset.removeAgent);if(!row)return;confirmRemoval("删除 Agent",`删除 ${row.definition.name} 后将停止接受新工作。历史和文件保留；未结束或待核对的工作会阻止删除。`,async()=>{await removeAgent(app,row.definition.id,row.revision);await app.render();app.refreshFrame();notify("Agent 已删除，历史和文件已保留。");});};
  for(const button of root.querySelectorAll("[data-agent-chat]"))button.onclick=()=>openAgent(app,button.dataset.agentChat);
  for(const button of root.querySelectorAll("[data-instance]"))button.onclick=()=>openInstance(app,button.dataset.instance);
  for(const button of root.querySelectorAll("[data-close-instance]"))button.onclick=()=>{const s=app.state;s.openTabs=s.openTabs.filter(id=>id!==button.dataset.closeInstance);if(s.selectedInstance?.id===button.dataset.closeInstance){s.selectedInstance=null;app.navigate("overview");}else app.refreshFrame();};
  for(const button of root.querySelectorAll("[data-run]"))button.onclick=()=>showRun(app,button.dataset.run);
  for(const button of root.querySelectorAll("[data-artifact]"))button.onclick=()=>previewArtifact(app,button.dataset.artifact);
  const actions={"new-project":app.createProject,"new-session":app.createSession,"open-agent":()=>chooseAgent(app),"add-agent":()=>app.openSettings(null,true),search:()=>quickJump(app),archive:()=>showArchive(app).catch(error=>notify(error.message,true)),upload:()=>uploadArtifact(app),"project-settings":()=>projectSettings(app),"session-settings":()=>sessionSettings(app),"more-projects":()=>app.loadMoreProjects().catch(error=>notify(error.message,true)),"more-sessions":()=>app.loadMoreSessions().catch(error=>notify(error.message,true))};
  for(const button of root.querySelectorAll("[data-action]"))if(actions[button.dataset.action])button.onclick=actions[button.dataset.action];
}
export function renderAgentCatalog(app,root){
  root.innerHTML=`<section class="overview"><div class="page-heading"><h1>Agent</h1><button class="icon-btn" data-action="add-agent" title="添加 Agent" aria-label="添加 Agent">${icon("plus")}</button></div><div class="agent-grid">${agentCards(app.state)}</div></section>`;
  bindWorkbench(app,root);
}
function chooseAgent(app){
  const s=app.state;if(!s.agents.length){app.openSettings(null,true);return;}if(!s.session){app.createSession();return;}
  const content=element(`<div>${s.agents.map(row=>`<button class="settings-agent" data-choose-agent="${escape(row.definition.id)}">${avatar(s,row.definition.id)}${escape(row.definition.name)}</button>`).join("")}</div>`),close=modal("选择 Agent",content);
  for(const button of content.querySelectorAll("[data-choose-agent]"))button.onclick=()=>{close();openAgent(app,button.dataset.chooseAgent);};
}
export async function openAgent(app,identity,{choose=false}={}){
  const s=app.state;if(!s.session){app.createSession();return;}
  const sessionId=s.session.id,projectId=s.project.id;
  if(!choose&&s.unread?.agents?.[identity]?.instances===0){await openDraft(app,identity);return;}
  try{
    const page=await rpc("instances.list",{session_id:sessionId,agent_id:identity,limit:25});
    if(s.session?.id!==sessionId||s.project?.id!==projectId)return;
    let instances=page.items,cursor=page.cursor;
    for(const instance of instances)if(!s.instances.some(value=>value.id===instance.id))s.instances.push(instance);
    if(!choose&&!instances.length){await openDraft(app,identity);return;}
    if(!choose&&instances.length===1&&cursor==null){await openInstance(app,instances[0].id);return;}
    const name=s.agents.find(row=>row.definition.id===identity)?.definition.name||"Agent";
    const content=element(`<div class="instance-dialog"><div class="instance-picker"></div><button class="text-link" id="more-agent-instances" hidden>更多实例</button><footer class="dialog-footer"><button class="icon-btn" id="create-independent-instance" title="新建独立对话" aria-label="新建独立对话">${icon("plus")}</button></footer></div>`),close=modal(`${name} · 选择实例`,content,{size:"medium"});
    const draw=()=>{
      $(".instance-picker",content).innerHTML=instances.map(instance=>{const run=s.runs.find(value=>value.instance_id===instance.id),state=instance.active_run_id?"running":run?.state;return `<button class="instance-option ${instance.active_run_id?"":"is-idle"}" data-choose-instance="${escape(instance.id)}"><div class="instance-option-heading"><strong>${escape(instance.name)}</strong>${unreadBadge(instance.unread_count)}${state?badge(state):'<span class="v2-state">尚未开始</span>'}</div><span class="instance-origin">${instance.role==="manager"?"manager":"独立对话"}${run?` · ${date(run.updated_at||run.created_at)}`:""}</span><span class="instance-preview">${markdown(run?.goal||"尚无消息")}</span></button>`;}).join("")||'<p class="quiet-state">暂无实例。</p>';
      $("#more-agent-instances",content).hidden=cursor==null;
      for(const button of content.querySelectorAll("[data-choose-instance]"))button.onclick=()=>{close();openInstance(app,button.dataset.chooseInstance).catch(error=>notify(error.message,true));};
    };
    draw();
    $("#more-agent-instances",content).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const page=await rpc("instances.list",{session_id:sessionId,agent_id:identity,limit:25,cursor});instances.push(...page.items);cursor=page.cursor;for(const instance of page.items)if(!s.instances.some(value=>value.id===instance.id))s.instances.push(instance);draw();}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
    $("#create-independent-instance",content).onclick=()=>{close();openDraft(app,identity);};
  }catch(error){notify(error.message,true);}
}
export async function openDraft(app,agentId){
  const s=app.state; s.conversationDrafts??=[];
  let draft=s.conversationDrafts.find(value=>value.session_id===s.session.id&&value.agent_id===agentId);
  if(!draft){const key=crypto.randomUUID();draft={id:"draft:"+key,agent_id:agentId,session_id:s.session.id,project_id:s.project.id,role:"main",draft:true,name:"新对话",creationName:"对话 · "+new Date().toLocaleString("zh-CN")+" · "+key.slice(0,8)};s.conversationDrafts.push(draft);}
  await openInstance(app,draft.id);
}
export async function openInstance(app,identity){
  const s=app.state;let instance=s.instances.find(value=>value.id===identity)||(s.conversationDrafts||[]).find(value=>value.id===identity);
  if(!instance)instance=await rpc("instances.get",{instance_id:identity});
  if(instance.project_id!==s.project.id)await app.selectProject(instance.project_id);
  if(instance.session_id!==s.session?.id){const session=s.sessions.find(value=>value.id===instance.session_id)||await rpc("sessions.get",{session_id:instance.session_id});if(session.archived)throw new Error("此会话已归档，请先从回收站恢复。");if(!s.sessions.some(value=>value.id===session.id))s.sessions.push(session);await app.selectSession(session.id);}
  s.selectedInstance=instance;if(!instance.draft&&!s.instances.some(value=>value.id===identity))s.instances.push(instance);
  if(!s.openTabs.includes(identity))s.openTabs.push(identity);await app.navigate("conversation");
}
function projectSettings(app){
  const s=app.state,content=element(`<form><label class="field">项目名称<input id="rename-project" required maxlength="80" value="${escape(s.project.name)}"></label><dl class="v2-kv"><dt>原件目录</dt><dd>${escape(s.project.folder||"尚未绑定")}</dd><dt>数据位置</dt><dd>${escape(s.status.data_dir)}</dd></dl><div class="inline-actions"><button type="button" class="icon-btn danger-text" id="delete-project" title="永久删除项目" aria-label="永久删除项目">${icon("trash")}</button><button class="btn primary">保存名称</button></div></form>`),close=modal("项目设置",content);
  content.onsubmit=async event=>{event.preventDefault();try{Object.assign(s.project,await rpc("projects.update",{project_id:s.project.id,name:$("#rename-project",content).value}));close();app.refreshFrame();await app.render();}catch(error){notify(error.message,true);}};
  $("#delete-project",content).onclick=()=>{close();deleteProject(app,s.project);};
}
function deleteProject(app,project){
  const key=crypto.randomUUID(),content=element(`<form><p>确认后将注销此项目的全部平台记录，包括归档会话、历史任务、未决动作和待核对用量，无法恢复。</p><p class="form-hint">本项目的当前执行会被撤销。项目原件目录和外部备份会保留；已经发出的外部动作不会因注销而自动回滚。</p><label class="field">输入项目名称“${escape(project.name)}”以确认<input id="delete-project-name" required autocomplete="off" spellcheck="false"></label><button class="icon-btn danger-text" type="submit" title="确认永久删除项目" aria-label="确认永久删除项目">${icon("trash")}</button></form>`),close=modal("永久删除项目",content);
  content.onsubmit=async event=>{event.preventDefault();const name=$("#delete-project-name",content).value;if(name!==project.name){notify("项目名称不一致，请输入完整项目名称。",true);return;}const button=content.querySelector("button");button.disabled=true;try{await rpc("projects.delete",{project_id:project.id,confirm_name:name,idempotency_key:key});close();await app.reloadProjects({deletedId:project.id});}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
}
function sessionSettings(app){
  const s=app.state;if(!s.session)return;const session=s.session,content=element(`<form><label class="field">会话名称<input id="rename-session" required maxlength="80" value="${escape(session.name)}"></label><p class="form-hint">归档保留对话、运行和产物；已有工作继续处理，新的工作须先恢复会话。</p><div class="inline-actions"><button type="button" class="btn" id="archive-session">${session.archived?"恢复会话":"归档会话"}</button><button class="btn primary">保存名称</button></div></form>`),close=modal("会话设置",content);
  content.onsubmit=async event=>{event.preventDefault();try{Object.assign(session,await rpc("sessions.update",{project_id:s.project.id,session_id:session.id,name:$("#rename-session",content).value}));close();app.refreshFrame();await app.render();}catch(error){notify(error.message,true);}};
  $("#archive-session",content).onclick=async()=>{try{Object.assign(session,await rpc("sessions.set_archived",{project_id:s.project.id,session_id:session.id,archived:!session.archived}));close();await app.reloadSessions();await app.navigate("overview");}catch(error){notify(error.message,true);}};
}
async function showArchive(app){
  const s=app.state,projectId=s.project?.id,content=element('<div></div>'),close=modal("回收站",content);
  let items=[],cursor=null,loading=false,closed=false;close.onclose=()=>{closed=true;};
  const draw=()=>{
    if(closed)return;
    content.innerHTML=`<h3>当前项目的已归档会话</h3>${items.map(value=>`<p>${escape(value.name)} <button class="btn" data-restore-session="${escape(value.id)}">恢复会话</button></p>`).join("")||`<p class="form-hint">${loading?"正在加载已归档会话…":projectId?"暂无已归档会话。":"选择项目后查看已归档会话。"}</p>`}${cursor!=null?`<button class="btn" id="more-archive" ${loading?"disabled":""}>${loading?"正在加载…":"加载更多会话"}</button>`:""}`;
    for(const button of content.querySelectorAll("[data-restore-session]"))button.onclick=async()=>{button.disabled=true;try{const restored=await rpc("sessions.set_archived",{project_id:projectId,session_id:button.dataset.restoreSession,archived:false});items=items.filter(value=>value.id!==restored.id);if(s.project?.id===projectId){await app.reloadSessions({restored});app.refreshFrame();await app.render();}draw();}catch(error){button.disabled=false;notify(error.message,true);}};
    if($("#more-archive",content))$("#more-archive",content).onclick=()=>load(true).catch(error=>notify(error.message,true));
  };
  const load=async more=>{if(loading)return;loading=true;draw();try{const page=await rpc("sessions.list",{project_id:projectId,archived:true,cursor:more?cursor:0,limit:25});items=more?[...items,...page.items]:page.items;cursor=page.cursor;}finally{loading=false;draw();}};
  if(projectId)await load(false);else draw();
}
function quickJump(app){
  const s=app.state,content=element('<div><label class="field">搜索或快速跳转<input id="quick-search" type="search" placeholder="项目、会话、Agent 或实例名称"></label><div id="quick-results"></div></div>'),close=modal("快速跳转",content);
  const draw=()=>{const query=$("#quick-search",content).value.toLocaleLowerCase(),items=[...s.projects.map(value=>({...value,kind:"project",label:"项目"})),...s.sessions.filter(value=>!value.archived).map(value=>({...value,kind:"session",label:"会话"})),...s.agents.map(row=>({...row.definition,kind:"agent",label:"Agent"})),...s.instances.map(value=>({...value,kind:"instance",label:"实例"}))];$("#quick-results",content).innerHTML=items.filter(value=>`${value.name} ${value.id}`.toLocaleLowerCase().includes(query)).map(value=>`<button class="settings-agent" data-quick-kind="${value.kind}" data-quick-id="${escape(value.id)}"><span>${escape(value.name)}<small>${value.label}</small></span></button>`).join("");for(const button of content.querySelectorAll("[data-quick-kind]"))button.onclick=async()=>{close();try{const {quickKind:kind,quickId:id}=button.dataset;if(kind==="project")await app.selectProject(id);if(kind==="session")await app.selectSession(id);if(kind==="agent")await openAgent(app,id);if(kind==="instance")await openInstance(app,id);}catch(error){notify(error.message,true);}};};$("#quick-search",content).oninput=draw;draw();$("#quick-search",content).focus();
}
