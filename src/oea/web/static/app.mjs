import { $,escape,icon,iconButton,rpc,element,modal,notify,empty } from "./ui.mjs";
import { renderSettings,selectSettingsAgent } from "./settings.mjs";
import { shell,overview,workspacePages } from "./workbench-shell.mjs";
import { bindWorkbench,renderAgentCatalog,renderArtifactsPage,renderConversation,disposeConversation,openInstance,openAgent } from "./workbench-pages.mjs";
import { renderPlugins,pluginTabs } from "./plugin-settings.mjs";
import { renderBox } from "./api-box.mjs";
import { manageApplications } from "./application-settings.mjs";

import { renderChat,refreshChat,disposeChat } from "./chat.mjs";
import { renderRuns,showRun } from "./runs.mjs";
import { renderWorkflows } from "./workflows.mjs";
import { renderAgentCanvas } from "./agent-canvas.mjs";
import { transitionView } from "./motion.mjs";

let browserTicket=new URLSearchParams(location.hash.slice(1)).get("oea-login");
if(browserTicket)history.replaceState(null,"",location.pathname+location.search+"#workspace");

function canvasRoute(){if(!location.hash.startsWith("#agent-canvas?"))return null;const params=new URLSearchParams(location.hash.split("?")[1]);return {project_id:params.get("project"),agent_id:params.get("agent"),architecture_id:params.get("draft"),version:params.get("version")?Number(params.get("version")):null};}
const initialCanvas=canvasRoute();
function locationPage(){
  const [hash,tab,id]=location.hash.slice(1).split("/");
  const page=({workspace:"overview",settings:"agent-settings",agents:"agents",plugins:"plugins","api-box":"api-box",workflows:"workflows",applications:"applications"})[hash]||"overview";
  return {page,pluginTab:Object.hasOwn(pluginTabs,tab)?tab:"installed",pluginId:page==="plugins"&&tab==="installed"&&id?id:null};
}
export const app={state:{canvasRoute:initialCanvas,...locationPage(),...(initialCanvas?{page:"agent-canvas"}:{}),projects:[],agents:[],instances:[],sessions:[],project:null,session:null,schema:null,status:null,epoch:0,openTabs:[],selectedInstance:null,runs:[],attentionRuns:[],artifacts:[],summary:{},unread:null,mcp:[],skills:[]},rpc};
app.runListeners=new Set();
let stream=null,refreshing=false,refreshAgain=false,sessionsChanged=false,instancesChanged=false,instanceLoad=0;
function frame(preserve=false) {
  document.body.classList.toggle("agent-canvas-open",app.state.page==="agent-canvas");
  const s=app.state,previous=preserve?$("#page"):null,focused=preserve?document.activeElement:null;
  const scrolls=previous?[...previous.querySelectorAll(".transcript,.v2-feed,.inspector,.settings-list")].map(node=>[node,node.scrollTop,node.scrollLeft]):[],pageTop=previous?.parentElement.scrollTop||0;
  $("#app").innerHTML=shell(s);
  if(previous){$("#page").replaceWith(previous);previous.parentElement.scrollTop=pageTop;for(const [node,top,left] of scrolls){node.scrollTop=top;node.scrollLeft=left;}}
  $("#project-select").onchange=event=>app.selectProject(event.target.value).catch(error=>notify(error.message,true));
  $("#workspace-select").onchange=event=>app.selectSession(event.target.value).catch(error=>notify(error.message,true));
  bindWorkbench(app,$("#app"));if(focused?.isConnected)focused.focus({preventScroll:true});
}
async function loadInstances(){
  const request=++instanceLoad;
  const s=app.state;if(!s.session){Object.assign(s,{instances:[],runs:[],attentionRuns:[],artifacts:[],artifactCursor:null,summary:{},unread:null});return;}
  const scope={session_id:s.session.id,project_id:s.project.id};
  const [instances,runs,artifacts,summary,attention]=await Promise.all([rpc("instances.list",{session_id:s.session.id,limit:100}),rpc("runs.list",{...scope,limit:25}),rpc("artifacts.list",{...scope,limit:25}),rpc("sessions.summary",scope),rpc("runs.list",{...scope,view:"attention",limit:4})]);
  if(request!==instanceLoad||s.project?.id!==scope.project_id||s.session?.id!==scope.session_id)return;
  // A list request may have started before the model write. Keep the newer selection receipt.
  const selected=s.selectedInstance;
  const row=selected && instances.items.find(item=>item.id===selected.id);
  if(row && row.model_revision<selected.model_revision){
    Object.assign(row,{model_revision:selected.model_revision,model_ref:selected.model_ref,model_summary:selected.model_summary});
  }
  Object.assign(s,{instances:instances.items,runs:runs.items,attentionRuns:attention.items,artifacts:artifacts.items,artifactCursor:artifacts.cursor,summary});
  app.applyUnread(summary.unread,false);
  if(s.selectedInstance&&!s.selectedInstance.draft)s.selectedInstance=s.instances.find(value=>value.id===s.selectedInstance.id)||s.selectedInstance;
}
async function loadProject(identity){
  disposeConversation();disposeChat();stream?.close();const s=app.state;s.project=s.projects.find(value=>value.id===identity)||null;const epoch=++s.epoch;
  if(s.project){const [agents,sessions,mcp,skills]=await Promise.all([rpc("agents.list",{project_id:identity,limit:100}),rpc("sessions.list",{project_id:identity,archived:false,limit:100}),rpc("mcp.list",{project_id:identity,limit:100}),rpc("skills.list",{project_id:identity,limit:100})]);if(s.epoch!==epoch)return;s.agents=agents.items;s.sessions=sessions.items.filter(value=>!value.archived);s.sessionCursor=sessions.cursor;s.mcp=mcp.items;s.skills=skills.items;s.openTabs=[];s.selectedInstance=null;s.session=s.sessions.find(value=>value.id===localStorage.getItem("oea-session:"+identity))||s.sessions[0]||null;localStorage.setItem("oea-project",identity);rememberSession();await loadInstances();}
  else{Object.assign(s,{agents:[],sessions:[],session:null,sessionCursor:null,instances:[],runs:[],attentionRuns:[],artifacts:[],artifactCursor:null,summary:{},unread:null,unread:null,mcp:[],skills:[],openTabs:[],selectedInstance:null});localStorage.removeItem("oea-project");}
  await transitionView(async()=>{frame();await app.render();});connect();
}
let agentsLoad=0;
app.reloadAgents=async()=>{
  const project=app.state.project,request=++agentsLoad;
  if(!project)return;
  const page=await rpc("agents.list",{project_id:project.id,limit:100});
  if(app.state.project===project&&request===agentsLoad)app.state.agents=page.items;
};
app.reloadInstances=loadInstances;
app.openInstance=identity=>openInstance(app,identity);
app.showRun=identity=>showRun(app,identity);
app.openAgent=(identity,options)=>openAgent(app,identity,options);
app.applyUnread=(value,render=true)=>{const s=app.state;if(!value||value.session_id!==s.session?.id||value.project_id!==s.project?.id)return;if(s.unread?.session_id===value.session_id&&s.unread.revision>value.revision)return;s.unread=value;if(render)app.refreshFrame();};
app.reloadUnread=async()=>{const s=app.state;if(!s.session)return;app.applyUnread(await rpc("sessions.unread",{session_id:s.session.id,project_id:s.project.id}));};
app.selectProject=async id=>{
  if(app.state.pluginId&&id!==app.state.project?.id){
    app.state.pluginId=null;
    history.replaceState(null,"","#plugins/installed");
  }
  await loadProject(id);
};
function rememberSession(){const s=app.state,key="oea-session:"+s.project.id;if(s.session)localStorage.setItem(key,s.session.id);else localStorage.removeItem(key);}
app.selectSession=async identity=>{disposeConversation();disposeChat();const s=app.state;s.session=s.sessions.find(value=>value.id===identity&&!value.archived)||null;s.openTabs=[];s.selectedInstance=null;rememberSession();await loadInstances();await app.navigate("overview");};
app.reloadSessions=async({restored=null}={})=>{
  const s=app.state,projectId=s.project?.id;if(!projectId)return;
  const page=await rpc("sessions.list",{project_id:projectId,archived:false,limit:100});
  if(s.project?.id!==projectId)return;
  const previous=s.session,items=page.items.filter(value=>!value.archived);
  for(const session of [previous,restored])if(session&&!session.archived&&!items.some(value=>value.id===session.id))items.push(session);
  s.sessions=items;s.sessionCursor=page.cursor;s.session=items.find(value=>value.id===previous?.id)||items.find(value=>value.id===restored?.id)||items[0]||null;
  if(previous?.id!==s.session?.id){s.openTabs=[];s.selectedInstance=null;}
  rememberSession();await loadInstances();
};
app.loadMoreProjects=async()=>{const s=app.state;if(s.projectCursor==null)return;const page=await rpc("projects.list",{cursor:s.projectCursor,limit:100});s.projects.push(...page.items);s.projectCursor=page.cursor;app.refreshFrame();};
app.reloadProjects=async({deletedId=null}={})=>{
  const s=app.state;if(deletedId){localStorage.removeItem("oea-session:"+deletedId);localStorage.removeItem("oea-events:"+deletedId);}
  const page=await rpc("projects.list",{limit:100});s.projects=page.items;s.projectCursor=page.cursor;
  await loadProject(page.items.find(value=>value.id===s.project?.id&&value.id!==deletedId)?.id||page.items[0]?.id);
};
app.loadMoreSessions=async()=>{const s=app.state,projectId=s.project.id;if(s.sessionCursor==null)return;const page=await rpc("sessions.list",{project_id:projectId,archived:false,cursor:s.sessionCursor,limit:100});if(s.project?.id!==projectId)return;s.sessions=[...new Map([...s.sessions,...page.items.filter(value=>!value.archived)].map(value=>[value.id,value])).values()];s.sessionCursor=page.cursor;app.refreshFrame();};
app.createSession=createSession;
app.createProject=createProject;
function pluginHash(tab,id){
  return "#plugins/"+tab+(id?"/"+encodeURIComponent(id):"");
}

let navigation=0;
app.navigate=async(page,{fromHistory=false,canvasRoute:nextCanvas=null,push=false,pluginTab="installed",pluginId=null,replace=false}={})=>{
  const request=++navigation;
  if(app.canvasController){if(!await app.canvasController.leave()){app.updateCanvasRoute();return;}app.canvasController.dispose();app.canvasController=null;}
  await transitionView(async()=>{
    if(request!==navigation)return;
    if(nextCanvas)app.state.canvasRoute=nextCanvas;
    if(push)history.pushState(null,"","#agent-canvas");
    disposeConversation();disposeChat();
    app.state.mobileNavigationOpen=false;
    app.state.page=page;
    app.state.pluginId=page==="plugins"&&pluginTab==="installed"?pluginId:null;
    if(page==="plugins")app.state.pluginTab=pluginTab;
    if(page==="agent-canvas")app.updateCanvasRoute();
    else if(!fromHistory){
      const hash=page==="plugins"?pluginHash(pluginTab,app.state.pluginId):workspacePages.has(page)?"#workspace":page==="agent-settings"?"#settings":"#"+page;
      const method=!replace&&hash!==location.hash&&(page==="plugins"||page==="api-box")?"pushState":"replaceState";
      const origin=app.state.pluginId&&location.hash==="#plugins/installed"?{pluginList:true}:null;
      history[method](origin,"",hash);
    }
    frame();await app.render();
  });
};
app.leavePluginSettings=()=>history.state?.pluginList?history.back():app.navigate("plugins",{replace:true});
app.updateCanvasRoute=()=>{const r=app.state.canvasRoute;if(!r)return;const params=new URLSearchParams({project:app.state.project.id,agent:r.agent_id});if(r.architecture_id)params.set("draft",r.architecture_id);if(r.version)params.set("version",r.version);history.replaceState(null,"","#agent-canvas?"+params);};
app.openAgentCanvas=route=>app.navigate("agent-canvas",{canvasRoute:{...route,project_id:app.state.project.id},push:true});
app.refreshFrame=()=>frame(true);
app.openSettings=async(id,create=false)=>{selectSettingsAgent(id,create);await app.navigate("agent-settings");};

app.render=async()=>{
  const s=app.state,root=$("#page");root.className="";root.dataset.view=s.page;
  if(s.page!=="conversation")disposeConversation();
  if(s.page==="api-box")return renderBox(app,root);
  if(s.page==="plugins")return renderPlugins(app,root);
  if(!s.project){root.innerHTML=empty("创建你的第一个项目","",'<button class="btn primary" data-action="new-project">新建项目</button>');bindWorkbench(app,root);return;}
  if(s.page==="agent-canvas")await renderAgentCanvas(app,root);
  else if(s.page==="agent-settings"){await app.reloadAgents();await renderSettings(app,root);}
  else if(s.page==="agents")renderAgentCatalog(app,root);
  else if(s.page==="applications")await manageApplications(app,root);
  else if(s.page==="workflows")await renderWorkflows(app,root);
  else if(s.page==="artifacts")renderArtifactsPage(app,root);
  else if(s.page==="conversation")await renderConversation(app,root);
  else if(s.page==="chat")await renderChat(app,root);
  else{root.innerHTML=overview(s);bindWorkbench(app,root);}
};
let globalStream;
function connectGlobal(){
  if(globalStream)return;
  const key="oea-global-events";
  globalStream=new EventSource("/api/events?"+new URLSearchParams({global_scope:true,after:localStorage.getItem(key)||"0"}));
  globalStream.onmessage=async event=>{
    const value=JSON.parse(event.data);if(value.kind!=="event")return;
    localStorage.setItem(key,String(value.position));
    try{
      const kind=value.event.kind,s=app.state;
      if(["model.saved","model.removed"].includes(kind)){
        if(s.page==="api-box")await app.onBoxEvent?.();
        if(s.project&&["overview","agents","conversation","agent-settings"].includes(s.page)){
          await app.reloadAgents();
          if(s.page==="conversation")await loadInstances();
          if(s.page==="agent-settings")await app.onAgentEvent?.(value.event);else await app.render();
        }
      }
      if(["plugin.saved","plugin.removed"].includes(kind)){
        if(s.page==="plugins")await app.onPluginEvent?.(value.event);
        if(s.page==="agent-settings")await app.onAgentEvent?.(value.event);
      }
    }catch(error){notify(error.message,true);}
  };
}
function connect(){connectGlobal();
  stream?.close();stream=null;const s=app.state;
  const paint=value=>{s.connectionState=value;const text=$("#connection"),label=({connected:"事件连接正常",disconnected:"事件连接中断，正在恢复",idle:"未选择项目"})[value]||"正在连接事件流";if(text){text.textContent=label;text.parentElement.title=label;$(".dot",text.parentElement).className="dot "+({connected:"online",disconnected:"offline",idle:"muted"}[value]||"connecting");}};
  if(!s.project){paint("idle");return;}const key="oea-events:"+s.project.id;
  const channel=new EventSource("/api/events?"+new URLSearchParams({project_id:s.project.id,after:localStorage.getItem(key)||"0"}));stream=channel;
  const state=value=>{if(stream===channel)paint(value);};
  state("connecting");channel.onopen=()=>state("connected");channel.onerror=()=>state("disconnected");
  channel.onmessage=event=>{if(stream!==channel)return;const value=JSON.parse(event.data);if(value.kind!=="event")return;localStorage.setItem(key,String(value.position));scheduleRefresh(value.event);};
}
async function scheduleRefresh(fact){
  for(const listener of app.runListeners)listener(fact);
  if(["plugin.saved","plugin.removed"].includes(fact.kind)){if(app.state.page==="plugins")await app.onPluginEvent?.(value.event);if(app.state.page==="agent-settings")await app.onAgentEvent?.(fact);return;}
  if(fact.kind==="model.completed"){
    if(fact.session_id===app.state.session?.id){try{await app.reloadUnread();if(app.state.page==="overview"){const root=$("#page");root.innerHTML=overview(app.state);bindWorkbench(app,root);}}catch(error){notify(error.message,true);}}
    return;
  }
  if(fact.kind==="message.read"||fact.kind==="instance.read"){
    if(fact.session_id===app.state.session?.id&&fact.data.consumer_id===localStorage.getItem("oea-consumer")){
      try{await app.reloadUnread();if(app.state.page==="overview"){const root=$("#page");root.innerHTML=overview(app.state);bindWorkbench(app,root);}}catch(error){notify(error.message,true);}
    }
    return;
  }
  if(fact.kind==="project.deleted"){
    try{await rpc("projects.get",{project_id:fact.project_id});}
    catch(error){if(error.code!=="NOT_FOUND"){notify(error.message,true);return;}await app.reloadProjects({deletedId:fact.project_id});}
    return;
  }
  if(fact.kind==="usage.corrected"){await loadInstances();if(app.state.page==="overview")await app.render();return;}
  if(fact.kind.startsWith("action.")||fact.kind.startsWith("mcp.call.")){app.onToolEvent?.(fact);return;}
  if(["architecture.created","architecture.saved","architecture.published","architecture.removed"].includes(fact.kind))app.onArchitectureEvent?.(fact);
  if(fact.kind.startsWith("architecture."))return;
  if(fact.kind==="agent.saved"||fact.kind==="agent.removed"){await app.reloadAgents();await app.onAgentEvent?.(fact);if(["agents","overview","conversation"].includes(app.state.page))await app.render();app.refreshFrame();return;}
  if(fact.kind==="mcp.saved"||fact.kind.startsWith("skill.")){
    if(!app.state.project||fact.project_id&&fact.project_id!==app.state.project.id)return;
    const mcp=fact.kind==="mcp.saved",handled=await(mcp?app.onMCPEvent?.(fact):app.onSkillEvent?.(fact));
    if(!handled){const page=await rpc(mcp?"mcp.list":"skills.list",{project_id:app.state.project.id,limit:100});app.state[mcp?"mcp":"skills"]=page.items;app.refreshFrame();}
    return;
  }
  if(fact.kind.startsWith("workflow.")){app.onWorkflowEvent?.(fact);app.onWorkflowRunEvent?.(fact);return;}
  if(!["message.","run.","instance.","session.","artifact."].some(prefix=>fact.kind.startsWith(prefix)))return;
  if(fact.kind.startsWith("session."))sessionsChanged=true;
  const s=app.state;
  if(fact.session_id===s.session?.id){
    if(fact.kind==="instance.created"||fact.kind==="instance.removed")instancesChanged=true;
    if(fact.kind==="instance.removed"){
      s.openTabs=s.openTabs.filter(id=>id!==fact.instance_id);
      if(s.selectedInstance?.id===fact.instance_id)s.selectedInstance={...s.selectedInstance,removed_at:fact.data.removed_at};
    }
  }
  if(refreshing){refreshAgain=true;return;}refreshing=true;
  try {do{refreshAgain=false;const updateSessions=sessionsChanged,updateInstances=instancesChanged;sessionsChanged=false;instancesChanged=false;const selected=s.session?.id;if(selected){const session=await rpc("sessions.get",{session_id:selected});if(s.session?.id===selected)s.session=session;}if(updateSessions||s.session?.archived){const switched=s.session?.archived;await app.reloadSessions();if(switched)s.page="overview";}else await loadInstances();if(s.page==="chat")await refreshChat(app);else if((updateInstances&&s.page==="overview")||(["overview","artifacts","conversation"].includes(s.page)&&!$(".v2-dialog-backdrop,.modal-backdrop")))await app.render();app.refreshFrame();}while(refreshAgain);}
  catch(error){notify(error.message,true);}finally{refreshing=false;}
}
function createProject(){const form=element(`<form><div class="field"><label for="project-name">项目名称</label><input id="project-name" required maxlength="80"></div><div class="field"><label for="project-folder">项目原件目录（可选）</label><input id="project-folder" title="原件默认只读"></div><p>${iconButton("plus","创建项目",{type:"submit",className:"primary"})}</p></form>`);const close=modal("新建项目",form);form.onsubmit=async event=>{event.preventDefault();try{const project=await rpc("projects.create",{name:$("#project-name",form).value,folder:$("#project-folder",form).value||null});app.state.projects.push(project);close();await loadProject(project.id);}catch(error){notify(error.message,true);}};}
function createSession(){if(!app.state.project)return;const form=element(`<form><div class="field"><label for="session-name">会话名称</label><input id="session-name" required maxlength="80" placeholder="例如：本轮开发协作"></div><p>${iconButton("plus","创建会话",{type:"submit",className:"primary"})}</p></form>`);const close=modal("新建会话",form);form.onsubmit=async event=>{event.preventDefault();try{const session=await rpc("sessions.create",{project_id:app.state.project.id,name:$("#session-name",form).value});localStorage.setItem("oea-session:"+app.state.project.id,session.id);close();await loadProject(app.state.project.id);}catch(error){notify(error.message,true);}};}
async function boot(){try{if(browserTicket){const ticket=browserTicket;browserTicket=null;const response=await fetch("/api/browser-login/redeem",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket})}),value=await response.json();if(!response.ok||value.error){const error=new Error(value.error?.safe_message||"Browser login failed");error.code=value.error?.code||"PERMISSION_DENIED";throw error;}}const [schema,status,projects]=await Promise.all([rpc("agents.schema"),rpc("host.status"),rpc("projects.list",{limit:100})]);Object.assign(app.state,{schema,status,projects:projects.items,projectCursor:projects.cursor});await loadProject(projects.items.find(value=>value.id===(initialCanvas?.project_id||localStorage.getItem("oea-project")))?.id||projects.items[0]?.id);}catch(error){$("#app").innerHTML=error.code==="PERMISSION_DENIED"?empty("请从本机启动器打开工作区","运行 oea open，或使用 Start-Workspace 启动器获取新的登录链接。") : empty("暂时无法连接工作区",error.message);}}
window.addEventListener("pagehide",()=>{stream?.close();disposeConversation();disposeChat();});
window.addEventListener("hashchange",()=>{if(new URLSearchParams(location.hash.slice(1)).has("oea-login")){location.reload();return;}const route=canvasRoute();if(route){app.navigate("agent-canvas",{fromHistory:true,canvasRoute:route}).catch(error=>notify(error.message,true));}else{const next=locationPage();app.navigate(next.page,{fromHistory:true,pluginTab:next.pluginTab,pluginId:next.pluginId}).catch(error=>notify(error.message,true));}});
window.addEventListener("keydown",event=>{if(event.key==="Escape"&&app.state.mobileNavigationOpen)$(".nav-scrim")?.click();if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="k"){event.preventDefault();if(!$(".v2-dialog-backdrop"))$('[data-action="search"]')?.click();}});boot();
