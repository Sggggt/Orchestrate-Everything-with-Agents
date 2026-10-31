import { $,escape,icon,rpc,element,notify,badge,date } from "./ui.mjs";
import { diagnosisHTML,integrityLabel } from "./diagnostics.mjs";
import { markdown } from "./markdown.mjs";
import { avatar } from "./workbench-shell.mjs";
import { showRun,showAction } from "./runs.mjs";
import { uploadArtifact } from "./artifact-directory.mjs";
import { observeRun,liveStates } from "./run-stream.mjs";
import { streamMarkdown } from "./stream-markdown.mjs";

const drafts=new Map(),attachments=new Map();
let active=null;
export const nearBottom=feed=>feed.scrollHeight-feed.scrollTop-feed.clientHeight<90;
export function mergeEntries(current,incoming){return [...new Map([...current,...incoming].map(entry=>[entry.id,entry])).values()].sort((a,b)=>a.position-b.position);}

export function conversationItems(entries){
  const items=[];
  for(const entry of entries){
    if(entry.role!=="user"&&!entry.text?.trim()){
      const previous=items.at(-1);
      if(previous?.kind==="activity"&&previous.runId===entry.run_id)previous.entries.push(entry);
      else items.push({key:entry.id,kind:"activity",runId:entry.run_id,entries:[entry]});
    }else items.push({key:entry.id,kind:entry.role,runId:entry.run_id,entry});
  }
  return items;
}
export function conversationItemHTML(item){
  if(item.kind==="activity")return `<details class="conversation-activity"><summary>${icon("terminal")}<span>执行活动${item.entries.length>1?` · ${item.entries.length} 轮`:""}</span>${icon("chevron")}</summary><div class="activity-receipts">${item.entries.filter(entry=>entry.action_id).map(entry=>`<div class="activity-receipt" data-activity="${escape(entry.action_id)}"><button class="text-link" data-original-action="${escape(entry.action_id)}">查看调用记录</button></div>`).join("")}</div></details>`;
  const entry=item.entry;
  return `<article class="conversation-message ${item.kind==="user"?"from-user":"from-agent"}">${item.kind==="user"?`<div class="conversation-time">你 · ${date(entry.created_at)}</div>`:""}<div class="message-text">${markdown(entry.text)}</div>${integrityLabel(entry.integrity)?`<small class="content-integrity">${integrityLabel(entry.integrity)}</small>`:""}${entry.action_id&&entry.characters>entry.text.length?`<button class="text-link full-response" data-original-action="${escape(entry.action_id)}">读取完整内容 ${icon("external")}</button>`:""}</article>`;
}

export function disposeConversation(){active?.close();active=null;}
export async function renderConversation(app,root){
  const s=app.state,instance=s.selectedInstance;if(!instance){disposeConversation();await app.navigate("overview");return;}
  root.className="conversation-page";
  if(active?.root===root&&active.instanceId===instance.id){await active.refresh();return;}
  disposeConversation();
  const projectId=s.project.id,sessionId=s.session.id,definition=s.agents.find(row=>row.definition.id===instance.agent_id)?.definition;
  const model=instance.role==="manager"?definition?.manager?.model:definition?.model;
  const modelLabel=model?.model||(instance.role==="manager"?"manager 未配置":definition?.execution.program.length?"本机执行端":"未配置模型");
  if(!drafts.has(instance.id))drafts.set(instance.id,{text:"",key:crypto.randomUUID()});
  const draft=drafts.get(instance.id);
  root.innerHTML=`<div class="chat-layout"><section class="chat-panel"><header class="chat-header">${avatar(s,instance.agent_id,true)}<div class="chat-identity"><h2>${escape(instance.name)}</h2><p>${escape(definition?.name||"Agent")} · ${instance.role==="manager"?"manager":"独立对话"}</p></div><div class="chat-header-actions"><button class="icon-btn" id="conversation-settings" title="配置 Agent" aria-label="配置 Agent">${icon("settings")}</button><span id="conversation-state"></span><button class="icon-btn" id="instance-inspector" aria-label="切换任务记录" aria-expanded="false">${icon("panel")}</button></div></header><div class="conversation transcript" aria-label="对话记录"><div class="transcript-inner"><button class="btn small load-more" id="older-conversation" hidden>加载更早的消息</button><div id="conversation-entries"></div><div id="conversation-live" aria-live="off"></div><div id="conversation-notice"></div></div></div><div class="composer-area"><button class="conversation-jump" id="conversation-latest" hidden>${icon("arrow")} 回到最新</button><form id="composer" class="composer"><textarea id="chat-input" aria-label="发送给 ${escape(instance.name)} 的消息" placeholder="与 ${escape(definition?.name||instance.name)} 一起工作…" rows="2">${escape(draft.text)}</textarea><div class="composer-footer"><div class="composer-tools"><button type="button" class="icon-btn" id="conversation-upload" title="添加工作产物" aria-label="上传产物">${icon("plus")}</button><span id="composer-status">Enter 发送 · Shift + Enter 换行</span></div><span class="composer-model" title="${escape(modelLabel)}">${escape(modelLabel)}</span><button class="composer-send" type="submit" aria-label="发送消息">${icon("arrow")}</button><button class="composer-stop" type="button" id="conversation-stop" aria-label="请求停止当前工作" hidden><span></span></button></div></form></div></section><aside class="inspector" aria-label="任务记录"><div class="inspector-section"><h3>任务记录</h3><div id="conversation-runs"></div><button class="text-link" id="older-runs" hidden>更多任务</button></div><details class="inspector-section"><summary>执行上下文</summary><dl class="v2-kv"><dt>实例</dt><dd>${escape(instance.id)}</dd><dt>模型</dt><dd>${escape(modelLabel)}</dd></dl></details></aside></div>`;
  const feed=$(".transcript",root),editor=$("#chat-input",root),container=$("#conversation-entries",root),live=$("#conversation-live",root),jump=$("#conversation-latest",root);
  let entries=[],before=null,runs=[],runCursor=null,first=true,closed=false,loading=false,again=false,stream=null,streamId=null,submitting=false,refreshTimer=null,liveValue=null,readPosition=0,marking=false;
  const nodes=new Map();
  const current=()=>!closed&&s.project?.id===projectId&&s.selectedInstance?.id===instance.id&&s.page==="conversation";
  function preserveScroll(update,prepend=false,force=false){
    const follow=nearBottom(feed),anchor=[...container.children].find(node=>node.getBoundingClientRect().bottom>feed.getBoundingClientRect().top),top=anchor?.getBoundingClientRect().top,oldHeight=feed.scrollHeight;
    update();
    if(first||(force||follow)&&!prepend){feed.scrollTop=feed.scrollHeight;first=false;jump.hidden=true;}
    else if(anchor?.isConnected)feed.scrollTop+=anchor.getBoundingClientRect().top-top;
    else if(prepend)feed.scrollTop+=feed.scrollHeight-oldHeight;
    jump.hidden=nearBottom(feed);
  }
  function draw(prepend=false,force=false){
    preserveScroll(()=>{
      const items=conversationItems(entries),keys=new Set();let cursor=container.firstElementChild;
      for(const item of items){
        keys.add(item.key);let record=nodes.get(item.key);
        if(!record){record={node:document.createElement("div"),item:null};record.node.dataset.entryId=item.key;nodes.set(item.key,record);}
        const old=record.item,unchanged=old?.kind===item.kind&&(item.kind==="activity"?old.entries.length===item.entries.length&&old.entries.every((entry,index)=>entry.action_id===item.entries[index].action_id):["text","characters","action_id","created_at","integrity"].every(key=>old.entry[key]===item.entry[key]));
        if(!unchanged){const open=record.node.querySelector("details")?.open;record.node.innerHTML=conversationItemHTML(item);record.item=item;if(open)record.node.querySelector("details").open=true;}
        if(record.node===cursor)cursor=cursor.nextElementSibling;
        else container.insertBefore(record.node,cursor);
      }
      for(const [key,record] of nodes)if(!keys.has(key)){record.node.remove();nodes.delete(key);}
      if(!items.length&&!container.querySelector(".conversation-empty"))container.innerHTML=`<div class="conversation-empty">${icon("spark")}<h2>从一个目标开始</h2><p>告诉 ${escape(definition?.name||instance.name)} 你想完成什么。</p></div>`;
      if(items.length)container.querySelector(".conversation-empty")?.remove();
      $("#older-conversation",root).hidden=before==null;
    },prepend,force);
    bindReceipts();
  }
  function bindReceipts(){
    for(const button of container.querySelectorAll("[data-original-action]"))button.onclick=()=>showAction(button.dataset.originalAction);
    for(const details of container.querySelectorAll(".conversation-activity"))details.ontoggle=async()=>{
      if(!details.open)return;
      for(const receipt of details.querySelectorAll("[data-activity]:not([data-loaded])")){
        receipt.dataset.loaded="true";
        try{const result=await rpc("runs.action",{action_id:receipt.dataset.activity,limit:8192});if(!current())return;
          if(result.next_offset!=null)continue;
          const calls=JSON.parse(result.text||"{}").message?.tool_calls||[];
          if(calls.length)receipt.prepend(element(`<div class="tool-call-list">${calls.map(call=>`<div>${icon("terminal")}<span>${escape(call.function?.name||"工具调用")}</span></div>`).join("")}</div>`));
        }catch(error){delete receipt.dataset.loaded;notify(error.message,true);}
      }
    };
  }
  function drawState(){
    const latest=runs[0],running=runs.find(run=>liveStates.has(run.state));
    $("#conversation-state",root).innerHTML=latest?badge(latest.state):"";
    $("#conversation-runs",root).innerHTML=runs.map(run=>`<button class="task-mini" data-conversation-run="${escape(run.id)}"><span class="title">${escape(run.display?.title||run.goal||"工作目标")}</span><span class="task-mini-meta">${badge(run.state)}<time>${date(run.created_at)}</time></span></button>`).join("")||'<p class="form-hint">暂无任务。</p>';
    $("#older-runs",root).hidden=runCursor==null;
    $("#conversation-notice",root).innerHTML=latest&&!running?`${diagnosisHTML(latest.error)}<div class="conversation-run-footer">${badge(latest.state)}<button class="text-link" data-conversation-run="${escape(latest.id)}">任务详情${icon("chevron")}</button></div>`:"";
    for(const button of root.querySelectorAll("[data-conversation-run]"))button.onclick=()=>showRun(app,button.dataset.conversationRun);
    for(const button of root.querySelectorAll("[data-evidence-action]"))button.onclick=()=>showAction(button.dataset.evidenceAction,button.dataset.evidenceAttempt||null);
    const stop=$("#conversation-stop",root),send=$(".composer-send",root);
    stop.hidden=!running;stop.disabled=!!running?.cancel_requested_at;send.hidden=!!running;send.disabled=submitting||!editor.value.trim();
    $("#composer-status",root).textContent=running?.cancel_requested_at?"停止请求已提交，等待执行确认":running?"正在工作 · 可先起草下一条消息":"Enter 发送 · Shift + Enter 换行";
    if((running?.id||null)!==streamId){
      stream?.close();stream=null;streamId=running?.id||null;
      if(running||latest?.state==="completed"){liveValue=null;live.innerHTML="";}
      if(running)stream=observeRun({runId:running.id,projectId,listeners:app.runListeners,readResponse:params=>rpc("runs.response",params),onSync:queueRefresh,onChange:value=>{
        if(!current())return;
        liveValue=value;preserveScroll(drawLive,false,true);
      }});
    }
  }
  function drawLive(){
    if(!liveValue)return;
    const value=liveValue,saved=value.actionId&&entries.some(entry=>entry.action_id===value.actionId);
    if(!live.querySelector(".live-response")){
      live.innerHTML='<div class="message-text live-response"></div><div class="stream-label" role="status"></div>';
      live.renderText=streamMarkdown(live.querySelector(".live-response"));
    }
    const body=live.querySelector(".live-response"),label=live.querySelector(".stream-label");
    body.hidden=!value.text||saved;
    if(!body.hidden)live.renderText(value.text,value.actionId+":"+value.attemptId);
    const note=value.note?`${value.connected&&(!value.run||liveStates.has(value.run.state))?'<span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>':icon("activity")}<span>${escape(value.note)}</span>`:"";
    if(label.innerHTML!==note)label.innerHTML=note;
  }
  function queueRefresh(){if(!closed&&refreshTimer===null)refreshTimer=setTimeout(()=>{refreshTimer=null;refresh().catch(error=>notify(error.message,true));},100);}
  async function refresh(){
    if(!current())return;if(instance.draft){draw();drawState();return;}if(loading){again=true;return;}loading=true;
    try{do{again=false;const [page,recent]=await Promise.all([rpc("instances.history",{instance_id:instance.id,project_id:projectId,limit:25}),rpc("runs.list",{session_id:sessionId,project_id:projectId,instance_id:instance.id,limit:25})]);if(!current())return;
      const changed=page.items.some(value=>!entries.some(old=>old.id===value.id&&old.text===value.text));
      if(!entries.length)before=page.cursor;entries=mergeEntries(entries,page.items);
      if(!runs.length)runCursor=recent.cursor;runs=[...new Map([...runs,...recent.items].map(run=>[run.id,run])).values()].sort((a,b)=>b.position-a.position);
      draw(false,changed);drawState();drawLive();await markRead();
    }while(again);}finally{loading=false;}
  }
  async function markRead(){
    if(marking||!current()||instance.draft||document.visibilityState!=="visible"||$(".v2-dialog-backdrop,.modal-backdrop")||!nearBottom(feed)||!entries.length)return;
    const position=entries.at(-1).position;if(position<=readPosition)return;marking=true;let succeeded=false;
    try{const result=await rpc("instances.mark_read",{instance_id:instance.id,project_id:projectId,position});readPosition=Math.max(readPosition,result.position);succeeded=true;if(current())app.applyUnread(result.summary);}
    catch(error){notify(error.message,true);}finally{marking=false;if(succeeded&&current()&&entries.at(-1)?.position>readPosition)markRead();}
  }
  const visible=()=>{if(document.visibilityState==="visible")markRead();};
  document.addEventListener("visibilitychange",visible);document.addEventListener("oea-modal-close",visible);
  active={root,instanceId:instance.id,refresh,close(){closed=true;clearTimeout(refreshTimer);stream?.close();document.removeEventListener("visibilitychange",visible);document.removeEventListener("oea-modal-close",visible);}};
  $("#older-conversation",root).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const older=await rpc("instances.history",{instance_id:instance.id,project_id:projectId,before,limit:25});if(!current())return;entries=mergeEntries(entries,older.items);before=older.cursor;draw(true);}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  $("#older-runs",root).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const page=await rpc("runs.list",{session_id:sessionId,project_id:projectId,instance_id:instance.id,cursor:runCursor,limit:25});if(!current())return;runs.push(...page.items);runCursor=page.cursor;drawState();}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  $("#conversation-settings",root).onclick=()=>app.openSettings(instance.agent_id);
  $("#instance-inspector",root).onclick=event=>{const expanded=$(".chat-layout",root).classList.toggle("show-inspector");event.currentTarget.setAttribute("aria-expanded",String(expanded));};
  jump.onclick=()=>{feed.scrollTop=feed.scrollHeight;jump.hidden=true;};
  feed.addEventListener("scroll",()=>{jump.hidden=nearBottom(feed);if(jump.hidden)markRead();},{passive:true});
  const resize=()=>{editor.style.height="auto";editor.style.height=Math.min(editor.scrollHeight,200)+"px";};
  editor.oninput=()=>{draft.text=editor.value;draft.key=crypto.randomUUID();for(const id of attachments.get(instance.id)?.keys()||[])if(!editor.value.includes(id))attachments.get(instance.id).delete(id);resize();$(".composer-send",root).disabled=submitting||!editor.value.trim();};
  editor.onkeydown=event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229){event.preventDefault();if(!runs.some(run=>liveStates.has(run.state)))$("#composer",root).requestSubmit();}};
  $("#conversation-upload",root).onclick=()=>uploadArtifact(app,artifact=>{
    let selected=attachments.get(instance.id);if(!selected){selected=new Map();attachments.set(instance.id,selected);}
    if(selected.size>=32&&!selected.has(artifact.id))throw new Error("文件已上传；一次工作最多可分配 32 个来源文件。");
    selected.set(artifact.id,artifact);draft.text+="\nArtifactRef: "+artifact.id+" ("+artifact.name+")";draft.key=crypto.randomUUID();if(current()){editor.value=draft.text;resize();$(".composer-send",root).disabled=false;}
  });
  $("#conversation-stop",root).onclick=async event=>{const run=runs.find(value=>liveStates.has(value.state)),button=event.currentTarget;if(!run)return;button.disabled=true;try{await rpc("runs.cancel",{run_id:run.id});notify("停止请求已提交，实际结果以执行回执为准。");await refresh();}catch(error){button.disabled=false;notify(error.message,true);}};
  $("#composer",root).onsubmit=async event=>{
    event.preventDefault();const input=editor.value;if(submitting||!input.trim()||runs.some(run=>liveStates.has(run.state)))return;
    const key=draft.key,artifact_ids=[...(attachments.get(instance.id)?.keys()||[])].filter(id=>input.includes(id));submitting=true;$(".composer-send",root).disabled=true;
    try{
      if(instance.draft){
        const oldId=instance.id,created=await rpc("instances.create",{session_id:sessionId,agent_id:instance.agent_id,name:instance.creationName});
        Object.assign(instance,created,{draft:false});drafts.delete(oldId);drafts.set(instance.id,draft);
        if(attachments.has(oldId)){attachments.set(instance.id,attachments.get(oldId));attachments.delete(oldId);}
        s.conversationDrafts=(s.conversationDrafts||[]).filter(value=>value!==instance);
        if(s.session?.id===sessionId){if(!s.instances.some(value=>value.id===instance.id))s.instances.push(instance);s.openTabs=s.openTabs.map(id=>id===oldId?instance.id:id);}
        if(active?.root===root)active.instanceId=instance.id;
        if(current()){$(".chat-identity h2",root).textContent=instance.name;app.refreshFrame();}
      }
      await rpc("runs.submit",{spec:{instance_id:instance.id,input,artifact_ids,idempotency_key:key}});if(draft.text===input){draft.text="";draft.key=crypto.randomUUID();}for(const id of artifact_ids)attachments.get(instance.id)?.delete(id);if(current()){editor.value=draft.text;resize();await refresh();feed.scrollTop=feed.scrollHeight;editor.focus();}await app.reloadInstances();app.refreshFrame();}
    catch(error){notify(error.message,true);}finally{submitting=false;if(current())$(".composer-send",root).disabled=!editor.value.trim();}
  };
  resize();await refresh();
}
