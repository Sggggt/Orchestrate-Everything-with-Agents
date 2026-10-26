import { $,escape,icon,rpc,element,modal,notify,empty,date } from "./ui.mjs";
import { markdown } from "./markdown.mjs";

const drafts=new Map();let active=null;
const purposes={inform:"资料消息",request:"工作请求",reply:"答复原工作",report:"进展与问题"};
function draft(session){if(!drafts.has(session))drafts.set(session,{content:"",purpose:"inform",recipients:[],reply:null,key:crypto.randomUUID()});return drafts.get(session);}
const nearBottom=feed=>feed.scrollHeight-feed.scrollTop-feed.clientHeight<90;
export function disposeChat(){if(active?.onVisible){document.removeEventListener("visibilitychange",active.onVisible);document.removeEventListener("oea-modal-close",active.onVisible);}active=null;}
export async function renderChat(app,root){
  disposeChat();
  const s=app.state;if(!s.session){root.innerHTML=empty("开始一次协作会话","把本轮目标、资料与各个实例的进展放在一起。",'<button class="btn primary" id="create-session">新建会话</button>');$("#create-session").onclick=app.createSession;active=null;return;}
  const d=draft(s.session.id);root.className="panel-page collaboration-page";
  root.innerHTML=`<div class="page-heading"><div><h1>协作群聊</h1></div><div class="v2-actions"><label class="v2-muted" for="collaboration-mode">协作模式</label><select id="collaboration-mode"><option value="direct">直接管理</option><option value="collaborative">manager 协作</option></select><button class="icon-btn" id="add-instance" title="添加实例" aria-label="添加实例">${icon("plus")}</button></div></div><section class="v2-chat"><div id="message-feed" class="conversation group-conversation v2-feed" role="log" aria-label="群聊消息"></div><button id="new-messages" class="v2-new-messages" hidden></button><form id="group-composer" class="composer v2-composer"><div class="v2-compose-tools"><label class="sr-only" for="message-purpose">消息用途</label><select id="message-purpose">${Object.entries(purposes).map(([key,label])=>`<option value="${key}">${label}</option>`).join("")}</select><label class="sr-only" for="message-target">添加接收对象</label><select id="message-target"></select><div id="recipient-chips" class="v2-actions"></div></div><div id="reply-strip"></div><label class="sr-only" for="message-content">消息正文</label><textarea id="message-content" maxlength="65536" placeholder="分享资料，或选择“工作请求”分配明确目标…" required></textarea><div class="v2-compose-bottom"><small id="purpose-hint">普通资料只入箱，不会唤醒全部 Agent。</small><button class="icon-btn" type="submit" title="发送消息" aria-label="发送消息">${icon("send")}</button></div></form></section>`;
  active={app,session:s.session.id,records:[],history:false,lastMarked:0,readPosition:0};
  active.onVisible=()=>{if(document.visibilityState==="visible"&&app.state.page==="chat"&&nearBottom($("#message-feed")))markRead(app);};
  document.addEventListener("visibilitychange",active.onVisible);document.addEventListener("oea-modal-close",active.onVisible);
  $("#collaboration-mode").value=s.session.mode;
  $("#collaboration-mode").onchange=async event=>{try{s.session=await rpc("sessions.set_mode",{session_id:s.session.id,mode:event.target.value,expected_epoch:s.session.epoch});updateTargets();notify("协作模式已更新，已接受的实例工作保留。");}catch(error){event.target.value=s.session.mode;notify(error.message,true);}};
  $("#add-instance").onclick=()=>addInstance(app);
  $("#message-purpose").value=d.purpose;$("#message-content").value=d.content;
  $("#message-content").oninput=event=>{d.content=event.target.value;d.key=crypto.randomUUID();};
  $("#message-content").onkeydown=event=>{if(event.key==="Enter"&&(event.ctrlKey||event.metaKey)){event.preventDefault();$(".v2-composer").requestSubmit();}};
  $("#message-purpose").onchange=event=>{if(event.target.value==="reply"&&!d.reply){event.target.value=d.purpose;notify("请先选择一条原工作消息，再点击答复。",true);return;}d.purpose=event.target.value;d.key=crypto.randomUUID();updateHint();};
  $("#message-target").onchange=event=>{const value=event.target.value;if(!value)return;if(value==="all")d.recipients=["all"];else d.recipients=[...new Set([...d.recipients.filter(item=>item!=="all"),value])];d.key=crypto.randomUUID();event.target.value="";updateChips();};
  $("#new-messages").onclick=()=>loadLatest(app);
  $("#message-feed").addEventListener("scroll",()=>{if(active&&!active.history&&nearBottom($("#message-feed")))markRead(app);},{passive:true});
  $(".v2-composer").onsubmit=async event=>{
    event.preventDefault();if(!d.recipients.length){notify("请先选择接收对象。",true);return;}
    const button=$(".v2-composer button[type=submit]");button.disabled=true;
    try{const spec={session_id:s.session.id,recipients:d.recipients,content:d.content,purpose:d.purpose,idempotency_key:d.key};if(d.reply){spec.reply_to=d.reply.id;if(d.reply.run_id)spec.target_run_id=d.reply.run_id;}
      const receipt=await rpc("messages.send",{spec});d.content="";d.reply=null;d.purpose="inform";d.key=crypto.randomUUID();$("#message-content").value="";$("#message-purpose").value="inform";updateReply();updateHint();await loadLatest(app);if(receipt.deliveries.some(item=>item.rejected_at))notify("消息已保存，部分原工作已结束，请查看回执。");
    }catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
  updateTargets();updateReply();updateHint();await loadLatest(app);
  function updateHint(){$("#purpose-hint").textContent=d.purpose==="request"?"工作请求会执行，并关联同一根预算。":d.purpose==="reply"?"答复应用到原工作，沿用原运行与预算。":"普通资料和报告只入箱。Ctrl / ⌘ + Enter 发送";}
  function updateTargets(){
    const options=[["all","@所有可见实例"],...s.instances.map(value=>["instance:"+value.id,"@"+value.name+" · "+(value.role==="manager"?"manager":"实例")])];
    if(s.session.mode==="collaborative")for(const row of s.agents)if(row.definition.manager)options.push(["department:"+row.definition.id,"@Agent · "+row.definition.name]);
    $("#message-target").innerHTML='<option value="">@ 添加接收对象</option>'+options.map(([id,label])=>`<option value="${escape(id)}">${escape(label)}</option>`).join("");
    $("#message-target").disabled=!!d.reply?.run_id;updateChips();renderMembers(app);
  }
  function updateChips(){const label=value=>value==="all"?"@所有实例":value.startsWith("department:")?"@Agent "+(s.agents.find(row=>row.definition.id===value.slice(11))?.definition.name||value.slice(11)):"@"+(s.instances.find(item=>item.id===value.replace("instance:",""))?.name||value);
    $("#recipient-chips").innerHTML=d.recipients.map((value,index)=>`<button type="button" class="v2-purpose" data-remove-target="${index}" ${d.reply?.run_id?"disabled":""}>${escape(label(value))} ×</button>`).join("");for(const button of document.querySelectorAll("[data-remove-target]"))button.onclick=()=>{d.recipients.splice(Number(button.dataset.removeTarget),1);d.key=crypto.randomUUID();updateChips();};}
  function updateReply(){$("#reply-strip").innerHTML=d.reply?`<div class="v2-reply-strip"><span>答复：${escape(d.reply.excerpt)}</span><button type="button" id="cancel-reply" aria-label="取消答复">${icon("close")}</button></div>`:"";if(d.reply)$("#cancel-reply").onclick=()=>{d.reply=null;d.purpose="inform";$("#message-purpose").value="inform";updateReply();updateTargets();updateHint();};$("#message-target").disabled=!!d.reply?.run_id;}
  active.updateTargets=updateTargets;
  active.reply=async message=>{try{const receipt=await rpc("messages.get",{message_id:message.id,project_id:s.project.id});const runs=receipt.deliveries.filter(item=>item.run_id&&!item.rejected_at);
    const choose=delivery=>{d.reply={id:message.id,excerpt:message.content.slice(0,90),run_id:delivery?.run_id};d.purpose=delivery?"reply":"inform";if(delivery)d.recipients=["instance:"+delivery.instance_id];d.key=crypto.randomUUID();$("#message-purpose").value=d.purpose;updateReply();updateTargets();updateHint();$("#message-content").focus();};
    if(runs.length>1){const content=element(`<div><p class="v2-muted">选择要接续的原工作。</p>${runs.map((item,index)=>`<button class="btn" data-choice="${index}">${escape(s.instances.find(value=>value.id===item.instance_id)?.name||item.instance_id)}</button>`).join(" ")}</div>`);const close=modal("答复原工作",content);for(const button of content.querySelectorAll("[data-choice]"))button.onclick=()=>{choose(runs[Number(button.dataset.choice)]);close();};}else choose(runs[0]);
  }catch(error){notify(error.message,true);}};
}
export function recipientLabels(message,state){
  const recipients=message.recipient_instance_ids||[];
  return recipients.length?recipients.map(id=>"@"+(state.instances.find(value=>value.id===id)?.name||id)):["未记录收件人"];
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
async function loadLatest(app){if(!active||app.state.page!=="chat"||!$("#message-feed"))return;const id=active.session;const page=await rpc("messages.list",{session_id:id,project_id:app.state.project.id,order:"desc",limit:100});if(active?.session!==id||app.state.page!=="chat")return;active.latestPosition=page.items[0]?.position||0;active.records=page.items.reverse();active.hasOlder=page.cursor!=null;active.history=false;renderFeed(app,true);await markRead(app);}
function renderFeed(app,bottom=false){const feed=$("#message-feed");if(!feed||!active)return;const scroll=feed.scrollTop;const s=app.state;
  feed.innerHTML=`${active.records.length?'<p><button class="btn" id="older-messages">查看更早消息</button></p>':empty("让协作从一个明确目标开始","选择接收对象后发送资料；需要执行时，明确选择“工作请求”。")}${active.records.map(message=>{const source=message.source,{name}=messageSource(message,s);const summary=message.delivery_summary||{};return `<article class="v2-message ${escape(source.kind)}">${sourceAvatar(message,s)}<div class="v2-message-main"><div class="v2-message-meta"><strong>${escape(name)}</strong><span class="v2-purpose">${purposes[message.purpose]}</span><time>${date(message.created_at)}</time></div><div class="message-recipients">${recipientLabels(message,s).map(label=>`<span>${escape(label)}</span>`).join("")}</div>${message.reply_to?'<div class="v2-muted">↳ 关联原讨论</div>':""}<div class="v2-bubble markdown">${markdown(message.content)}</div><div class="v2-message-foot"><span>入箱 ${summary.accepted||0} · 应用 ${summary.applied||0} · 完成 ${summary.completed||0}${summary.rejected?" · 未应用 "+summary.rejected:""}</span><button data-reply="${escape(message.id)}" title="答复" aria-label="答复">${icon("reply")}</button><button data-receipt="${escape(message.id)}" title="消息回执" aria-label="消息回执">${icon("list")}</button></div></div></article>`;}).join("")}`;
  for(const button of feed.querySelectorAll("[data-message-agent]"))button.onclick=()=>app.openAgent(button.dataset.messageAgent,{choose:true});
  for(const button of feed.querySelectorAll("[data-message-instance]"))button.onclick=()=>app.openInstance(button.dataset.messageInstance).catch(error=>notify(error.message,true));
  if(!active.hasOlder)$("#older-messages",feed)?.parentElement.remove();
  feed.querySelectorAll(".v2-message-foot").forEach((footer,index)=>{const stamp=document.createElement("span");stamp.className="v2-read-state";stamp.dataset.readPosition=String(active.records[index].position);stamp.title="当前浏览器的用户阅读位置，与 Agent 应用或工作完成无关。";footer.append(stamp);});
  updateReadStamps();
  if(bottom)feed.scrollTop=feed.scrollHeight;else feed.scrollTop=scroll;
  for(const button of feed.querySelectorAll("[data-reply]"))button.onclick=()=>active.reply(active.records.find(value=>value.id===button.dataset.reply));
  for(const button of feed.querySelectorAll("[data-receipt]"))button.onclick=async()=>{try{const receipt=await rpc("messages.get",{message_id:button.dataset.receipt,project_id:s.project.id});modal("消息回执",element(`<div><p class="v2-muted">入箱、开始、应用、完成与用户已读分别记录。</p><pre class="v2-pre">${escape(JSON.stringify(receipt,null,2))}</pre></div>`));}catch(error){notify(error.message,true);}};
  if($("#older-messages"))$("#older-messages").onclick=async()=>{try{const page=await rpc("messages.list",{session_id:active.session,project_id:s.project.id,order:"desc",before:active.records[0].position,limit:100});if(!page.items.length){active.hasOlder=false;renderFeed(app);notify("已到最早的消息。");return;}active.records=page.items.reverse();active.hasOlder=page.cursor!=null;active.history=true;renderFeed(app);feed.scrollTop=0;const button=$("#new-messages");button.hidden=false;button.textContent="返回最新消息";}catch(error){notify(error.message,true);}};
}
function updateReadStamps(){if(!active)return;for(const stamp of document.querySelectorAll("[data-read-position]"))stamp.textContent=Number(stamp.dataset.readPosition)<=active.readPosition?"我已读":"未标记已读";}
async function markRead(app){
  if(!active||active.history||!active.records.length||app.state.page!=="chat"||document.visibilityState!=="visible"||$(".v2-dialog-backdrop,.modal-backdrop")||!$("#message-feed"))return;
  const view=active,position=view.records.at(-1).position;if(position<=view.lastMarked)return;view.lastMarked=position;
  try{const read=await rpc("messages.mark_read",{session_id:view.session,consumer_id:localStorage.getItem("oea-consumer"),position,project_id:app.state.project.id});view.readPosition=Math.max(view.readPosition,read.position);if(active!==view)return;app.applyUnread(read.summary);updateReadStamps();const button=$("#new-messages");if(button)button.hidden=true;}
  catch(error){view.lastMarked=0;notify(error.message,true);}
}
export async function refreshChat(app){
  if(!active||active.session!==app.state.session?.id||!$("#message-feed"))return;
  const view=active;
  const page=await rpc("messages.list",{session_id:view.session,project_id:app.state.project.id,order:"desc",limit:100});
  if(active!==view||app.state.page!=="chat"||!$("#message-feed"))return;
  const latest=page.items[0]?.position||0,newMessage=latest>(view.latestPosition||0);
  if(newMessage){view.history=false;view.latestPosition=latest;view.records=page.items.reverse();view.hasOlder=page.cursor!=null;renderFeed(app,true);await markRead(app);}
  else{
    const changes=new Map(page.items.map(value=>[value.id,value]));view.records=view.records.map(value=>changes.get(value.id)||value);renderFeed(app);
    const unread=await rpc("messages.unread",{session_id:view.session,consumer_id:localStorage.getItem("oea-consumer"),project_id:app.state.project.id});
    if(active!==view||app.state.page!=="chat")return;view.readPosition=unread.position;updateReadStamps();
    const button=$("#new-messages");button.hidden=!unread.count&&!view.history;button.textContent=view.history?"返回最新消息":unread.count+" 条未读消息";
  }
  view.updateTargets?.();const mode=$("#collaboration-mode");if(mode)mode.value=app.state.session.mode;
}
function addInstance(app){const s=app.state;const content=element(`<form><div class="field"><label for="instance-agent">业务能力</label><select id="instance-agent">${s.agents.filter(row=>row.definition.enabled).map(row=>`<option value="${escape(row.definition.id)}">${escape(row.definition.name)}</option>`).join("")}</select></div><div class="field"><label for="instance-name">实例名称</label><input id="instance-name" required maxlength="256" placeholder="例如：文档审阅 01"></div><p class="v2-muted">继承已保存的模型、工具、Skill 与工作环境。添加实例不会发起模型调用。</p><p><button class="btn primary">添加实例</button></p></form>`);const close=modal("添加工作实例",content);content.onsubmit=async event=>{event.preventDefault();try{await rpc("instances.create",{session_id:s.session.id,agent_id:$("#instance-agent",content).value,name:$("#instance-name",content).value});close();await app.reloadInstances();await renderChat(app,$("#page"));}catch(error){notify(error.message,true);}};}
