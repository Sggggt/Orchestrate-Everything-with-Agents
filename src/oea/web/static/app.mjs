import { $,escape,icon,rpc,element,modal,notify,empty } from "./ui.mjs";
import { renderSettings,selectSettingsAgent } from "./settings.mjs";
import { shell,overview,workspacePages } from "./workbench-shell.mjs";
import { bindWorkbench,renderAgentCatalog,renderArtifactsPage,renderConversation,disposeConversation,openInstance,openAgent } from "./workbench-pages.mjs";
import { manageMCP } from "./mcp-settings.mjs";
import { manageSkills } from "./skill-settings.mjs";
import { renderChat,refreshChat,disposeChat } from "./chat.mjs";
import { renderRuns } from "./runs.mjs";
import { renderWorkflows } from "./workflows.mjs";

export const app={state:{page:({workspace:"overview",settings:"agent-settings",agents:"agents",mcp:"mcp",skills:"skills",workflows:"workflows"}[location.hash.slice(1)]||"overview"),projects:[],agents:[],instances:[],sessions:[],project:null,session:null,schema:null,status:null,epoch:0,openTabs:[],selectedInstance:null,runs:[],artifacts:[],summary:{},unread:null,mcp:[],skills:[]},rpc};
app.runListeners=new Set();
let stream=null,refreshing=false,refreshAgain=false,sessionsChanged=false;
function frame(preserve=false) {
  const s=app.state,previous=preserve?$("#page"):null,focused=preserve?document.activeElement:null;
  const scrolls=previous?[...previous.querySelectorAll(".transcript,.v2-feed,.inspector,.settings-list")].map(node=>[node,node.scrollTop,node.scrollLeft]):[],pageTop=previous?.parentElement.scrollTop||0;
  $("#app").innerHTML=shell(s);
  if(previous){$("#page").replaceWith(previous);previous.parentElement.scrollTop=pageTop;for(const [node,top,left] of scrolls){node.scrollTop=top;node.scrollLeft=left;}}
  $("#project-select").onchange=event=>loadProject(event.target.value).catch(error=>notify(error.message,true));
  $("#workspace-select").onchange=event=>app.selectSession(event.target.value).catch(error=>notify(error.message,true));
  bindWorkbench(app,$("#app"));if(focused?.isConnected)focused.focus({preventScroll:true});
}
async function loadInstances(){
  const s=app.state;if(!s.session){Object.assign(s,{instances:[],runs:[],artifacts:[],artifactCursor:null,summary:{},unread:null});return;}
  const scope={session_id:s.session.id,project_id:s.project.id};
  const [instances,runs,artifacts,summary]=await Promise.all([rpc("instances.list",{session_id:s.session.id,limit:100}),rpc("runs.list",{...scope,limit:25}),rpc("artifacts.list",{...scope,limit:25}),rpc("sessions.summary",scope)]);
  if(s.project?.id!==scope.project_id||s.session?.id!==scope.session_id)return;
  Object.assign(s,{instances:instances.items,runs:runs.items,artifacts:artifacts.items,artifactCursor:artifacts.cursor,summary});
  app.applyUnread(summary.unread,false);
  if(s.selectedInstance&&!s.selectedInstance.draft)s.selectedInstance=s.instances.find(value=>value.id===s.selectedInstance.id)||s.selectedInstance;
}
async function loadProject(identity){
  disposeConversation();disposeChat();stream?.close();const s=app.state;s.project=s.projects.find(value=>value.id===identity)||null;const epoch=++s.epoch;
  if(s.project){const [agents,sessions,mcp,skills]=await Promise.all([rpc("agents.list",{project_id:identity,limit:100}),rpc("sessions.list",{project_id:identity,archived:false,limit:100}),rpc("mcp.list",{project_id:identity,limit:100}),rpc("skills.list",{project_id:identity,limit:100})]);if(s.epoch!==epoch)return;s.agents=agents.items;s.sessions=sessions.items.filter(value=>!value.archived);s.sessionCursor=sessions.cursor;s.mcp=mcp.items;s.skills=skills.items;s.openTabs=[];s.selectedInstance=null;s.session=s.sessions.find(value=>value.id===localStorage.getItem("oea-session:"+identity))||s.sessions[0]||null;localStorage.setItem("oea-project",identity);rememberSession();await loadInstances();}
  else{Object.assign(s,{agents:[],sessions:[],session:null,sessionCursor:null,instances:[],runs:[],artifacts:[],artifactCursor:null,summary:{},unread:null,unread:null,mcp:[],skills:[],openTabs:[],selectedInstance:null});localStorage.removeItem("oea-project");}
  frame();await app.render();connect();
}
app.reloadAgents=async()=>{app.state.agents=(await rpc("agents.list",{project_id:app.state.project.id,limit:100})).items;};
app.reloadInstances=loadInstances;
app.openInstance=identity=>openInstance(app,identity);
app.openAgent=(identity,options)=>openAgent(app,identity,options);
app.applyUnread=(value,render=true)=>{const s=app.state;if(!value||value.session_id!==s.session?.id||value.project_id!==s.project?.id)return;if(s.unread?.session_id===value.session_id&&s.unread.revision>value.revision)return;s.unread=value;if(render)app.refreshFrame();};
app.reloadUnread=async()=>{const s=app.state;if(!s.session)return;app.applyUnread(await rpc("sessions.unread",{session_id:s.session.id,project_id:s.project.id}));};
app.selectProject=loadProject;
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
app.navigate=async page=>{disposeConversation();disposeChat();app.state.mobileNavigationOpen=false;app.state.page=page;history.replaceState(null,"",workspacePages.has(page)?"#workspace":page==="agent-settings"?"#settings":"#"+page);frame();await app.render();};
app.refreshFrame=()=>frame(true);
app.openSettings=async(id,create=false)=>{selectSettingsAgent(id,create);await app.navigate("agent-settings");};

app.render=async()=>{
  const s=app.state,root=$("#page");root.className="";
  if(s.page!=="conversation")disposeConversation();
  if(!s.project){root.innerHTML=empty("创建你的第一个项目","把 Agent、协作会话和业务图放在同一个工作空间里。",'<button class="btn primary" data-action="new-project">新建项目</button>');bindWorkbench(app,root);return;}
  if(s.page==="agent-settings"){await app.reloadAgents();await renderSettings(app,root);}
  else if(s.page==="agents")renderAgentCatalog(app,root);
  else if(s.page==="mcp")await manageMCP(app,root);
  else if(s.page==="skills")await manageSkills(app,root);
  else if(s.page==="workflows")await renderWorkflows(app,root);
  else if(s.page==="artifacts")renderArtifactsPage(app,root);
  else if(s.page==="conversation")await renderConversation(app,root);
  else if(s.page==="chat")await renderChat(app,root);
  else{root.innerHTML=overview(s);bindWorkbench(app,root);}
};
function connect(){stream?.close();const s=app.state;if(!s.project)return;const key="oea-events:"+s.project.id;stream=new EventSource("/api/events?"+new URLSearchParams({project_id:s.project.id,after:localStorage.getItem(key)||"0"}));stream.onopen=()=>{$("#connection").textContent="已连接";};stream.onerror=()=>{$("#connection").textContent="连接中断，等待恢复";};stream.onmessage=event=>{const value=JSON.parse(event.data);if(value.kind!=="event")return;localStorage.setItem(key,String(value.position));scheduleRefresh(value.event);};}
async function scheduleRefresh(fact){
  for(const listener of app.runListeners)listener(fact);
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
  if(fact.kind.startsWith("action.")||fact.kind.startsWith("mcp.call.")){app.onToolEvent?.(fact);return;}
  if(fact.kind==="agent.saved"||fact.kind==="agent.removed"){await app.reloadAgents();await app.onAgentEvent?.(fact);if(["agents","overview"].includes(app.state.page))await app.render();app.refreshFrame();return;}
  if(fact.kind==="mcp.saved"||fact.kind.startsWith("skill.")){
    if(!app.state.project||fact.project_id&&fact.project_id!==app.state.project.id)return;
    const mcp=fact.kind==="mcp.saved",handled=await(mcp?app.onMCPEvent?.(fact):app.onSkillEvent?.(fact));
    if(!handled){const page=await rpc(mcp?"mcp.list":"skills.list",{project_id:app.state.project.id,limit:100});app.state[mcp?"mcp":"skills"]=page.items;app.refreshFrame();}
    return;
  }
  if(fact.kind.startsWith("workflow.")){app.onWorkflowEvent?.(fact);app.onWorkflowRunEvent?.(fact);return;}
  if(!["message.","run.","instance.","session.","artifact."].some(prefix=>fact.kind.startsWith(prefix)))return;
  if(fact.kind.startsWith("session."))sessionsChanged=true;
  if(refreshing){refreshAgain=true;return;}refreshing=true;
  try {do{refreshAgain=false;const updateSessions=sessionsChanged;sessionsChanged=false;const selected=app.state.session?.id;if(selected){const session=await rpc("sessions.get",{session_id:selected});if(app.state.session?.id===selected)app.state.session=session;}if(updateSessions||app.state.session?.archived){const switched=app.state.session?.archived;await app.reloadSessions();if(switched)app.state.page="overview";}else await loadInstances();if(app.state.page==="chat")await refreshChat(app);else if(["overview","artifacts","conversation"].includes(app.state.page)&&!$(".v2-dialog-backdrop,.modal-backdrop"))await app.render();app.refreshFrame();}while(refreshAgain);}
  catch(error){notify(error.message,true);}finally{refreshing=false;}
}
function createProject(){const form=element('<form><div class="field"><label for="project-name">项目名称</label><input id="project-name" required maxlength="80"></div><div class="field"><label for="project-folder">项目原件目录（可选）</label><input id="project-folder"><small>原件默认只读，实例在独立工作目录保存成果。</small></div><p><button class="btn primary">创建项目</button></p></form>');const close=modal("新建项目",form);form.onsubmit=async event=>{event.preventDefault();try{const project=await rpc("projects.create",{name:$("#project-name",form).value,folder:$("#project-folder",form).value||null});app.state.projects.push(project);close();await loadProject(project.id);}catch(error){notify(error.message,true);}};}
function createSession(){if(!app.state.project)return;const form=element('<form><div class="field"><label for="session-name">会话名称</label><input id="session-name" required maxlength="80" placeholder="例如：本轮开发协作"></div><p><button class="btn primary">创建会话</button></p></form>');const close=modal("新建会话",form);form.onsubmit=async event=>{event.preventDefault();try{const session=await rpc("sessions.create",{project_id:app.state.project.id,name:$("#session-name",form).value});localStorage.setItem("oea-session:"+app.state.project.id,session.id);close();await loadProject(app.state.project.id);}catch(error){notify(error.message,true);}};}
async function boot(){try{const [schema,status,projects]=await Promise.all([rpc("agents.schema"),rpc("host.status"),rpc("projects.list",{limit:100})]);Object.assign(app.state,{schema,status,projects:projects.items,projectCursor:projects.cursor});await loadProject(projects.items.find(value=>value.id===localStorage.getItem("oea-project"))?.id||projects.items[0]?.id);}catch(error){$("#app").innerHTML=empty("暂时无法连接工作区",error.message);}}
window.addEventListener("pagehide",()=>{stream?.close();disposeConversation();disposeChat();});
window.addEventListener("keydown",event=>{if(event.key==="Escape"&&app.state.mobileNavigationOpen)$(".nav-scrim")?.click();if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="k"){event.preventDefault();if(!$(".v2-dialog-backdrop"))$('[data-action="search"]')?.click();}});boot();
