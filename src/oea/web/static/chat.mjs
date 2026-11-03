import { $,escape,icon,rpc,element,modal,notify,empty,date } from "./ui.mjs";
import { markdown } from "./markdown.mjs";

const drafts=new Map();let active=null;
function draft(session){if(!drafts.has(session))drafts.set(session,{content:"",board_id:null,recipients:[],reply:null,key:crypto.randomUUID()});return drafts.get(session);}
const nearBottom=feed=>feed.scrollHeight-feed.scrollTop-feed.clientHeight<90;
export function disposeChat(){if(active?.onVisible){document.removeEventListener("visibilitychange",active.onVisible);document.removeEventListener("oea-modal-close",active.onVisible);}active=null;}
export async function renderChat(app,root){
  disposeChat();
  const s=app.state;if(!s.session){root.innerHTML=empty("开始一次协作会话","把本轮目标、资料与各个实例的进展放在一起。",'<button class="btn primary" id="create-session">新建会话</button>');$("#create-session").onclick=app.createSession;active=null;return;}
  const sessionId=s.session.id,projectId=s.project.id,d=draft(sessionId);
  let boardPage=await rpc("boards.list",{project_id:projectId,session_id:sessionId,limit:100});
  let general=boardPage.items.find(board=>board.scope==="session");
  while(!general&&boardPage.cursor!=null){boardPage=await rpc("boards.list",{project_id:projectId,session_id:sessionId,cursor:boardPage.cursor,limit:100});general=boardPage.items.find(board=>board.scope==="session");}
  if(s.session?.id!==sessionId||s.project?.id!==projectId||s.page!=="chat")return;
  d.board_id=d.reply?.board_id||general?.id;
  root.className="panel-page collaboration-page";
  root.innerHTML=`<div class="page-heading"><div><h1>协作群聊</h1></div><div class="v2-actions"><label class="v2-muted" for="collaboration-mode">协作模式</label><select id="collaboration-mode"><option value="direct">直接管理</option><option value="collaborative">manager 协作</option></select><button class="icon-btn" id="add-instance" title="添加实例" aria-label="添加实例">${icon("plus")}</button></div></div><section class="v2-chat"><div id="message-feed" class="conversation group-conversation v2-feed" role="log" aria-label="群聊消息"></div><button id="new-messages" class="v2-new-messages" hidden></button><form id="group-composer" class="composer v2-composer"><div class="v2-compose-tools"><label class="sr-only" for="message-target">添加接收对象</label><select id="message-target"></select><div id="recipient-chips" class="v2-actions"></div></div><div id="reply-strip"></div><label class="sr-only" for="message-content">消息正文</label><textarea id="message-content" maxlength="65536" placeholder="分享资料、进展或补充说明…" required></textarea><div class="v2-compose-bottom"><small id="purpose-hint">消息按时间统一展示。需要执行时，请打开实例对话提交工作。</small><button class="icon-btn" type="submit" title="发送消息" aria-label="发送消息">${icon("send")}</button></div></form></section>`;
  active={app,session:s.session.id,project:s.project.id,records:[],history:false,lastMarked:0,readPositions:{}};
  active.onVisible=()=>{if(document.visibilityState==="visible"&&app.state.page==="chat"&&nearBottom($("#message-feed")))markRead(app);};
  document.addEventListener("visibilitychange",active.onVisible);document.addEventListener("oea-modal-close",active.onVisible);
  $("#collaboration-mode").value=s.session.mode;
  $("#collaboration-mode").onchange=async event=>{try{s.session=await rpc("sessions.set_mode",{session_id:s.session.id,mode:event.target.value,expected_epoch:s.session.epoch});updateTargets();notify("协作模式已更新，已接受的实例工作保留。");}catch(error){event.target.value=s.session.mode;notify(error.message,true);}};
  $("#add-instance").onclick=()=>addInstance(app);
  $("#message-content").value=d.content;
  $("#message-content").oninput=event=>{d.content=event.target.value;d.key=crypto.randomUUID();};
  $("#message-content").onkeydown=event=>{if(event.key==="Enter"&&(event.ctrlKey||event.metaKey)){event.preventDefault();$(".v2-composer").requestSubmit();}};
  $("#message-target").onchange=event=>{const value=event.target.value;if(!value)return;if(value==="all")d.recipients=s.instances.map(item=>item.id);else d.recipients=[...new Set([...d.recipients,value])];d.key=crypto.randomUUID();event.target.value="";updateChips();};
  $("#new-messages").onclick=()=>loadLatest(app);
  $("#message-feed").addEventListener("scroll",()=>{if(active&&!active.history&&nearBottom($("#message-feed")))markRead(app);},{passive:true});
  $(".v2-composer").onsubmit=async event=>{
    event.preventDefault();
    const button=$(".v2-composer button[type=submit]");button.disabled=true;
    try{const spec={board_id:d.board_id,mentions:d.recipients,content:d.content,idempotency_key:d.key};if(d.reply)spec.reply_to=d.reply.id;
      const receipt=await rpc("messages.send",{spec});d.content="";d.reply=null;d.board_id=general?.id;d.key=crypto.randomUUID();$("#message-content").value="";updateReply();await loadLatest(app);if(receipt.issues.length)notify("留言已保存，部分提及或附件未送达，请查看回执。");
    }catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
  updateTargets();updateReply();await loadLatest(app);
  function updateHint(){$("#purpose-hint").textContent=(d.reply?.private?"回复保留原私聊范围；":"发送到群聊；")+"@ 只发送通知。Ctrl / ⌘ + Enter 发送";}
  function updateTargets(){
    const options=[["all","@当前会话的所有实例"],...s.instances.map(value=>[value.id,"@"+value.name])];
    $("#message-target").innerHTML='<option value="">@ 提及成员（可选）</option>'+options.map(([id,label])=>`<option value="${escape(id)}">${escape(label)}</option>`).join("");
    updateChips();renderMembers(app);
  }
  function updateChips(){const label=value=>"@"+(s.instances.find(item=>item.id===value)?.name||value);
    $("#recipient-chips").innerHTML=d.recipients.map((value,index)=>`<button type="button" class="v2-purpose" data-remove-target="${index}">${escape(label(value))} ×</button>`).join("");for(const button of document.querySelectorAll("[data-remove-target]"))button.onclick=()=>{d.recipients.splice(Number(button.dataset.removeTarget),1);d.key=crypto.randomUUID();updateChips();};}
  function updateReply(){$("#reply-strip").innerHTML=d.reply?`<div class="v2-reply-strip"><span>${d.reply.private?"回复私聊":"回复"}：${escape(d.reply.excerpt)}</span><button type="button" id="cancel-reply" aria-label="取消回复">${icon("close")}</button></div>`:"";if(d.reply)$("#cancel-reply").onclick=()=>{d.reply=null;d.board_id=general?.id;d.key=crypto.randomUUID();updateReply();};updateHint();}
  active.updateTargets=updateTargets;
  active.reply=async message=>{d.reply={id:message.id,board_id:message.board_id,private:message.board?.scope==="inbox",excerpt:message.content.slice(0,90)};d.board_id=message.board_id;d.key=crypto.randomUUID();updateReply();$("#message-content").focus();};
}

export function recipientLabels(message,state){
  const recipients=new Map((message.deliveries||[]).map(item=>[item.instance_id,{name:item.instance_name,failed:false}]));
  for(const issue of message.issues||[])if(issue.field==="mentions"&&!recipients.has(issue.target))recipients.set(issue.target,{failed:true});
  if(message.board?.scope==="inbox"&&!recipients.has(message.board.owner_instance_id))recipients.set(message.board.owner_instance_id,{name:message.board.owner_name,failed:true});
  if(message.board?.scope==="inbox"&&!recipients.get(message.board.owner_instance_id).name)recipients.get(message.board.owner_instance_id).name=message.board.owner_name;
  return [message.board?.scope==="inbox"?"私聊":"群聊",...[...recipients].map(([id,value])=>"@"+(state.instances.find(item=>item.id===id)?.name||value.name||id)+(value.failed?"（未送达）":""))];
}
export function messageSource(message,state){
  const source=message.source,instance=state.instances.find(value=>value.id===source.instance_id);
  const name=source.name||instance?.name||state.agents.find(row=>row.definition.id===source.agent_id)?.definition.name||(source.kind==="leader"?"leader":"Agent");
  return {name,agentId:source.agent_id||instance?.agent_id,instanceId:source.instance_id,kind:source.kind};
}
function sourceAvatar(message,state){
  const source=messageSource(message,state),target=source.kind==="manager"&&source.agentId?`data-message-agent="${escape(source.agentId)}"`:source.instanceId?`data-message-instance="${escape(source.instanceId)}"`:"";
  return target?`<button class="v2-avatar ${escape(source.kind)}" ${target} title="${source.kind==="manager"?"选择实例":"打开对话"}" aria-label="${escape(source.name)} · ${source.kind==="manager"?"选择实例":"打开对话"}">${escape(source.name.slice(0,1))}</button>`:`<span class="v2-avatar ${escape(source.kind)}">${escape(source.name.slice(0,1))}</span>`;
}
function renderMembers(app){const s=app.state,root=$("#members");if(!root)return;root.innerHTML=s.instances.map(value=>`<div class="v2-member"><span class="v2-avatar">${escape(value.name.slice(0,1))}</span><strong>${escape(value.name)}</strong></div>`).join("");}
async function loadLatest(app){if(!active||app.state.page!=="chat"||!$("#message-feed"))return;const view=active;const page=await rpc("messages.list",{session_id:view.session,project_id:view.project,order:"desc",limit:100});if(active!==view||app.state.page!=="chat")return;view.latestPosition=page.items[0]?.position||0;view.records=page.items.reverse();view.hasOlder=page.cursor!=null;view.history=false;renderFeed(app,true);await markRead(app);}
function renderFeed(app,bottom=false){const feed=$("#message-feed");if(!feed||!active)return;const scroll=feed.scrollTop;const s=app.state;
  feed.innerHTML=`${active.records.length?'<p><button class="btn" id="older-messages">查看更早消息</button></p>':empty("让协作从一个明确目标开始","在板块中分享资料和进展；打开实例对话可以提交明确工作。")}${active.records.map(message=>{const source=message.source,{name}=messageSource(message,s);const deliveries=message.deliveries||[],issues=message.issues||[];return `<article class="v2-message ${escape(source.kind)}">${sourceAvatar(message,s)}<div class="v2-message-main"><div class="v2-message-meta"><strong>${escape(name)}</strong><time>${date(message.created_at)}</time></div><div class="message-recipients">${recipientLabels(message,s).map(label=>`<span>${escape(label)}</span>`).join("")}</div>${message.reply_to?'<div class="v2-muted">↳ 关联原讨论</div>':""}<div class="v2-bubble markdown">${markdown(message.content)}</div><div class="v2-message-foot"><span>通知 ${deliveries.length} · 已读 ${deliveries.filter(item=>item.read_at).length}${issues.length?" · 未送达或无效引用 "+issues.length:""}</span><button data-reply="${escape(message.id)}" title="答复" aria-label="答复">${icon("reply")}</button><button data-receipt="${escape(message.id)}" title="消息回执" aria-label="消息回执">${icon("list")}</button></div></div></article>`;}).join("")}`;
  for(const button of feed.querySelectorAll("[data-message-agent]"))button.onclick=()=>app.openAgent(button.dataset.messageAgent,{choose:true});
  for(const button of feed.querySelectorAll("[data-message-instance]"))button.onclick=()=>app.openInstance(button.dataset.messageInstance).catch(error=>notify(error.message,true));
  if(!active.hasOlder)$("#older-messages",feed)?.parentElement.remove();
  feed.querySelectorAll(".v2-message-foot").forEach((footer,index)=>{const stamp=document.createElement("span");stamp.className="v2-read-state";stamp.dataset.readPosition=String(active.records[index].position);stamp.dataset.readBoard=active.records[index].board_id;stamp.title="当前浏览器的用户阅读位置，与 Agent 阅读或工作完成分别记录。";footer.append(stamp);});
  updateReadStamps();
  if(bottom)feed.scrollTop=feed.scrollHeight;else feed.scrollTop=scroll;
  for(const button of feed.querySelectorAll("[data-reply]"))button.onclick=()=>active.reply(active.records.find(value=>value.id===button.dataset.reply));
  for(const button of feed.querySelectorAll("[data-receipt]"))button.onclick=async()=>{try{const receipt=await rpc("messages.get",{message_id:button.dataset.receipt,project_id:s.project.id});modal("消息回执",element(`<div><p class="v2-muted">正文已保存；通知送达和已读分别记录。已读不表示工作已完成。</p><pre class="v2-pre">${escape(JSON.stringify(receipt,null,2))}</pre></div>`));}catch(error){notify(error.message,true);}};
  if($("#older-messages"))$("#older-messages").onclick=async()=>{const view=active;try{const page=await rpc("messages.list",{session_id:view.session,project_id:view.project,order:"desc",before:view.records[0].position,limit:100});if(active!==view)return;if(!page.items.length){view.hasOlder=false;renderFeed(app);notify("已到最早的消息。");return;}view.records=page.items.reverse();view.hasOlder=page.cursor!=null;view.history=true;renderFeed(app);feed.scrollTop=0;const button=$("#new-messages");button.hidden=false;button.textContent="返回最新消息";}catch(error){notify(error.message,true);}};
}
function updateReadStamps(){if(!active)return;for(const stamp of document.querySelectorAll("[data-read-position]"))stamp.textContent=Number(stamp.dataset.readPosition)<=(active.readPositions[stamp.dataset.readBoard]||0)?"我已读":"未标记已读";}
async function markRead(app){
  if(!active||active.history||!active.records.length||app.state.page!=="chat"||document.visibilityState!=="visible"||$(".v2-dialog-backdrop,.modal-backdrop")||!$("#message-feed"))return;
  const view=active,position=view.records.at(-1).position;if(position<=view.lastMarked)return;view.lastMarked=position;
  try{const read=await rpc("messages.mark_read",{board_id:null,session_id:view.session,consumer_id:localStorage.getItem("oea-consumer"),position,project_id:view.project});view.readPositions=read.positions;if(active!==view)return;app.applyUnread(read.summary);updateReadStamps();const button=$("#new-messages");if(button)button.hidden=true;}
  catch(error){view.lastMarked=0;notify(error.message,true);}
}
export async function refreshChat(app){
  if(!active||active.session!==app.state.session?.id||!$("#message-feed"))return;
  const view=active;
  const page=await rpc("messages.list",{session_id:view.session,project_id:view.project,order:"desc",limit:100});
  if(active!==view||app.state.page!=="chat"||!$("#message-feed"))return;
  const latest=page.items[0]?.position||0,newMessage=latest>(view.latestPosition||0);
  if(newMessage){view.history=false;view.latestPosition=latest;view.records=page.items.reverse();view.hasOlder=page.cursor!=null;renderFeed(app,true);await markRead(app);}
  else{
    const changes=new Map(page.items.map(value=>[value.id,value]));view.records=view.records.map(value=>changes.get(value.id)||value);renderFeed(app);
    const unread=await rpc("messages.unread",{board_id:null,session_id:view.session,consumer_id:localStorage.getItem("oea-consumer"),project_id:view.project});
    if(active!==view||app.state.page!=="chat")return;view.readPositions=unread.positions;updateReadStamps();
    const button=$("#new-messages");button.hidden=!unread.count&&!view.history;button.textContent=view.history?"返回最新消息":unread.count+" 条未读消息";
  }
  view.updateTargets?.();const mode=$("#collaboration-mode");if(mode)mode.value=app.state.session.mode;
}
function addInstance(app){const s=app.state;const content=element(`<form><div class="field"><label for="instance-agent">业务能力</label><select id="instance-agent">${s.agents.filter(row=>row.definition.enabled).map(row=>`<option value="${escape(row.definition.id)}">${escape(row.definition.name)}</option>`).join("")}</select></div><div class="field"><label for="instance-name">实例名称</label><input id="instance-name" required maxlength="256" placeholder="例如：文档审阅 01"></div><p class="v2-muted">继承已保存的模型、工具、Skill 与工作环境。添加实例不会发起模型调用。</p><p><button class="btn primary">添加实例</button></p></form>`);const close=modal("添加工作实例",content);content.onsubmit=async event=>{event.preventDefault();try{await rpc("instances.create",{session_id:s.session.id,agent_id:$("#instance-agent",content).value,name:$("#instance-name",content).value});close();await app.reloadInstances();await renderChat(app,$("#page"));}catch(error){notify(error.message,true);}};}
