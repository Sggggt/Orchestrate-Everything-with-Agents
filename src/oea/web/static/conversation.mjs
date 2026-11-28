import { listChoices } from "./api-box.mjs";
import { $,escape,icon,rpc,element,modal,notify,badge,date } from "./ui.mjs";
import { diagnosisHTML,integrityLabel } from "./diagnostics.mjs";
import { markdown } from "./markdown.mjs";
import { avatar } from "./workbench-shell.mjs";
import { showRun,showAction } from "./runs.mjs";
import { uploadArtifact } from "./artifact-directory.mjs";
import { observeRun,liveStates } from "./run-stream.mjs";
import { streamMarkdown } from "./stream-markdown.mjs";

const drafts=new Map(),attachments=new Map();
export async function applyModelChoice(app,{instance,agent,model,agentDefault=false}){
  if(instance.role==="manager"||instance.removed_at)throw new Error("此实例的模型选择为只读。");
  const model_ref=model?.id||null,project_id=instance.project_id;
  if(agentDefault){
    await app.rpc("agents.set_model",{agent_id:instance.agent_id,project_id,model_ref,expected_revision:agent.revision,role:"main"});
    await app.reloadAgents();return;
  }
  if(instance.draft){Object.assign(instance,{model_ref,model_summary:model});return;}
  const saved=await app.rpc("instances.set_model",{instance_id:instance.id,project_id,model_ref,expected_revision:instance.model_revision});
  for(const target of new Set([instance,app.state?.selectedInstance,...(app.state?.instances||[])])){
    if(target?.id===saved.id && target.model_revision<=saved.model_revision)Object.assign(target,saved);
  }
}
const historyPageSize=100;
let active=null;
export const nearBottom=feed=>feed.scrollHeight-feed.scrollTop-feed.clientHeight<90;
export function mergeEntries(current,incoming){return [...new Map([...current,...incoming].map(entry=>[entry.id,entry])).values()].sort((a,b)=>a.position-b.position);}

export async function completeMessageText(entry,read){
  const parts=[entry.text];
  let offset=entry.next_offset;
  while(offset!=null){
    const page=await read(entry.id,offset);
    parts.push(page.text);
    offset=page.next_offset;
  }
  return {...entry,text:parts.join(""),next_offset:null};
}

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
  return `<article class="conversation-message ${item.kind==="user"?"from-user":"from-agent"}">${item.kind==="user"?`<div class="conversation-time">你 · ${date(entry.created_at)}</div>`:""}<div class="message-text">${markdown(entry.text)}</div>${integrityLabel(entry.integrity)?`<small class="content-integrity">${integrityLabel(entry.integrity)}</small>`:""}</article>`;
}

export function disposeConversation(){active?.close();active=null;}
export async function renderConversation(app,root){
  const s=app.state,instance=s.selectedInstance;if(!instance){disposeConversation();await app.navigate("overview");return;}
  root.className="conversation-page";
  const removed=Boolean(instance.removed_at);
  if(active?.root===root&&active.instanceId===instance.id&&active.removed===removed){await active.refresh();return;}
  disposeConversation();
  const projectId=s.project.id,sessionId=s.session.id,definition=s.agents.find(row=>row.definition.id===instance.agent_id)?.definition;
  const agentRow=()=>s.agents.find(row=>row.definition.id===instance.agent_id);
  const selectedModel=()=>{
    const latest=s.selectedInstance;
    if(latest.model_ref)return latest.model_summary;
    const role=latest.role==="manager"?"manager":"main";
    return agentRow()?.model_summary?.[role]??latest.model_summary;
  };
  const requiresModel=()=>{const execution=instance.role==="manager"?definition?.manager?.execution||definition?.execution:definition?.execution;return !execution?.program?.length||execution.program_uses_model;};
  const modelReady=()=>!requiresModel()||Boolean(selectedModel()?.enabled&&selectedModel()?.configured);
  const modelLabel=()=>!requiresModel()?"本机执行端":selectedModel()?.name?(selectedModel().name+(modelReady()?"":" · 不可用")):"请选择可用模型";
  const selectable=!removed&&instance.role!=="manager"&&requiresModel();
  if(!drafts.has(instance.id))drafts.set(instance.id,{text:"",key:crypto.randomUUID()});
  const draft=drafts.get(instance.id);
  root.innerHTML=`<div class="chat-layout"><section class="chat-panel"><header class="chat-header">${avatar(s,instance.agent_id,true)}<div class="chat-identity"><h2>${escape(instance.name)}</h2><p>${escape(definition?.name||"Agent")} · ${instance.role==="manager"?"manager":"独立对话"}</p></div><div class="chat-header-actions"><button class="icon-btn" id="conversation-settings" title="配置 Agent" aria-label="配置 Agent">${icon("settings")}</button><span id="conversation-state"></span><button class="icon-btn" id="instance-inspector" aria-label="切换任务记录" aria-expanded="false">${icon("panel")}</button></div></header><div class="conversation transcript" aria-label="对话记录"><div class="transcript-inner"><button class="btn small load-more" id="older-conversation" hidden>加载更早的消息</button><div id="conversation-entries"></div><div id="conversation-live" aria-live="off"></div><div id="conversation-notice"></div></div></div><div class="composer-area"><button class="conversation-jump" id="conversation-latest" hidden>${icon("arrow")} 回到最新</button><form id="composer" class="composer"><textarea id="chat-input" aria-label="发送给 ${escape(instance.name)} 的消息" placeholder="输入消息…" rows="2">${escape(draft.text)}</textarea><div class="composer-footer"><div class="composer-tools"><button type="button" class="icon-btn" id="conversation-upload" title="添加工作产物" aria-label="上传产物">${icon("plus")}</button><span id="composer-status"></span></div>${selectable?'<button type="button" class="btn composer-model" data-choose-model aria-label="选择下次任务的模型"></button>':'<span class="composer-model"></span>'}<button class="composer-send" type="submit" aria-label="发送消息">${icon("arrow")}</button><button class="composer-stop" type="button" id="conversation-stop" aria-label="请求停止当前工作" hidden><span></span></button></div></form></div></section><aside class="inspector" aria-label="任务记录"><div class="inspector-section"><h3>任务记录</h3><div id="conversation-runs"></div><button class="text-link" id="older-runs" hidden>更多任务</button></div><details class="inspector-section"><summary>执行上下文</summary><dl class="v2-kv"><dt>实例</dt><dd>${escape(instance.id)}</dd><dt>最近任务的模型</dt><dd data-run-model>暂无记录</dd></dl></details></aside></div>`;
  const feed=$(".transcript",root),editor=$("#chat-input",root),container=$("#conversation-entries",root),live=$("#conversation-live",root),jump=$("#conversation-latest",root);
  if(removed){$("#composer",root).classList.add("is-read-only");editor.readOnly=true;}
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
  function draw({prepend=false,force=false,updateRun=false}={}){
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
      if(!items.length&&!container.querySelector(".conversation-empty"))container.innerHTML=`<div class="conversation-empty">${icon("spark")}<h2>${escape(definition?.name||instance.name)}</h2></div>`;
      if(items.length)container.querySelector(".conversation-empty")?.remove();
      $("#older-conversation",root).hidden=before==null;
      if(updateRun){drawState();drawLive();}
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
    $(".composer-model",root).textContent=modelLabel();
    $("[data-run-model]",root).textContent=latest?.model_summary?.name||"暂无记录";
    $("#conversation-state",root).innerHTML=latest?badge(latest.state):"";
    $("#conversation-runs",root).innerHTML=runs.map(run=>`<button class="task-mini" data-conversation-run="${escape(run.id)}"><span class="title">${escape(run.display?.title||run.goal||"工作目标")}</span><span class="task-mini-meta">${badge(run.state)}<time>${date(run.created_at)}</time></span></button>`).join("")||'<p class="form-hint">暂无任务。</p>';
    $("#older-runs",root).hidden=runCursor==null;
    $("#conversation-notice",root).innerHTML=latest&&!running?`${diagnosisHTML(latest.error)}<div class="conversation-run-footer">${badge(latest.state)}<button class="text-link" data-conversation-run="${escape(latest.id)}">任务详情${icon("chevron")}</button></div>`:"";
    for(const button of root.querySelectorAll("[data-conversation-run]"))button.onclick=()=>showRun(app,button.dataset.conversationRun);
    for(const button of root.querySelectorAll("[data-evidence-action]"))button.onclick=()=>showAction(button.dataset.evidenceAction,button.dataset.evidenceAttempt||null);
    const stop=$("#conversation-stop",root),send=$(".composer-send",root);
    stop.hidden=!running;stop.disabled=!!running?.cancel_requested_at;send.hidden=!!running;send.disabled=submitting||!editor.value.trim()||!modelReady();
    $("#composer-status",root).textContent=removed?"实例已删除":running?.cancel_requested_at?"停止请求已提交，等待执行确认":running?"正在工作":"";
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
    const output=value.text?value:value.previous;
    if(!live.querySelector(".live-response")){
      live.innerHTML='<div class="message-text live-response"></div><div class="stream-label" role="status"></div>';
      live.renderText=streamMarkdown(live.querySelector(".live-response"));
    }
    const body=live.querySelector(".live-response"),label=live.querySelector(".stream-label");
    body.hidden=!output||saved;
    if(!body.hidden)live.renderText(output.text,output.actionId+":"+output.attemptId);
    const note=value.note?`${value.connected&&(!value.run||liveStates.has(value.run.state))?'<span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>':icon("activity")}<span>${escape(value.note)}</span>`:"";
    if(label.innerHTML!==note)label.innerHTML=note;
  }
  async function readHistory(bounds={}){
    const scope={instance_id:instance.id,project_id:projectId};
    const page=await rpc("instances.history",{...scope,...bounds,limit:historyPageSize,message_page:true});
    const pending=page.items.filter(entry=>entry.next_offset!=null);
    if(!pending.length||!current())return page;
    const bodies=new Map();
    const read=(entry_id,offset)=>rpc("instances.message",{...scope,entry_id,offset});
    let next=0;
    await Promise.all(Array.from({length:Math.min(pending.length,4)},async()=>{
      while(next<pending.length&&current()){
        const entry=await completeMessageText(pending[next++],read);
        bodies.set(entry.id,entry);
      }
    }));
    return {...page,items:page.items.map(entry=>bodies.get(entry.id)||entry)};
  }
  async function readNewHistory(after){
    const page=await readHistory(after==null?{}:{after});
    if(after==null)return page;
    const items=[...page.items];
    let cursor=page.cursor;
    while(cursor!=null&&current()){
      const earlier=await readHistory({after,before:cursor});
      items.push(...earlier.items);
      cursor=earlier.cursor;
    }
    return {items,cursor:null};
  }
  function queueRefresh(){if(!closed&&refreshTimer===null)refreshTimer=setTimeout(()=>{refreshTimer=null;refresh().catch(error=>notify(error.message,true));},100);}
  async function refresh(){
    if(!current())return;if(instance.draft){draw({updateRun:true});return;}if(loading){again=true;return;}loading=true;
    try{do{again=false;const [page,recent]=await Promise.all([readNewHistory(entries.at(-1)?.position),rpc("runs.list",{session_id:sessionId,project_id:projectId,instance_id:instance.id,limit:25})]);if(!current())return;
      const changed=page.items.length>0;
      if(!entries.length)before=page.cursor;entries=mergeEntries(entries,page.items);
      if(!runs.length)runCursor=recent.cursor;runs=[...new Map([...runs,...recent.items].map(run=>[run.id,run])).values()].sort((a,b)=>b.position-a.position);
      draw({force:changed,updateRun:true});await markRead();
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
  active={root,instanceId:instance.id,removed,refresh,close(){closed=true;clearTimeout(refreshTimer);stream?.close();document.removeEventListener("visibilitychange",visible);document.removeEventListener("oea-modal-close",visible);}};
  $("#older-conversation",root).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const older=await readHistory({before});if(!current())return;entries=mergeEntries(entries,older.items);before=older.cursor;draw({prepend:true});}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  $("#older-runs",root).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const page=await rpc("runs.list",{session_id:sessionId,project_id:projectId,instance_id:instance.id,cursor:runCursor,limit:25});if(!current())return;runs.push(...page.items);runCursor=page.cursor;drawState();}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  if(selectable)$("[data-choose-model]",root).onclick=async()=>{
    try{
      const models=await listChoices(app,"models.list");if(!current())return;
      const selected=s.selectedInstance,row=agentRow(),defaultName=row.model_summary?.main?.name||"未选择";
      const choice=(id,label,summary)=>{
        const chosen=(selected.model_ref||"")===id;
        return `<div class="model-choice-row"><button type="button" class="model-choice" data-pick="${escape(id)}" aria-pressed="${chosen}">
          <span class="choice-check">${chosen?icon("check"):""}</span><span>${escape(label)}</span></button>
          ${summary?`<button type="button" class="icon-btn" data-set-default="${escape(id)}" title="将 ${escape(label)} 设为 Agent 默认" aria-label="将 ${escape(label)} 设为 Agent 默认">${icon("sliders")}</button>`:""}</div>`;
      };
      const content=element(`<div class="model-choices">${choice("","跟随默认 · "+defaultName,false)}${models.map(model=>choice(model.id,model.name,true)).join("")}
        ${models.length?"":'<button type="button" class="btn" data-box>打开 API Box</button>'}</div>`);
      const close=modal("选择模型",content);
      let saving=false;
      content.onclick=async event=>{
        const button=event.target.closest("button");
        if(!button||saving||!current())return;
        if(button.hasAttribute("data-box")){await close();await app.navigate("api-box");return;}
        const agentDefault=button.hasAttribute("data-set-default");
        const id=agentDefault?button.dataset.setDefault:button.dataset.pick;
        saving=true;content.setAttribute("aria-busy","true");
        try{
          await applyModelChoice(app,{instance:s.selectedInstance,agent:agentRow(),model:models.find(item=>item.id===id)||null,agentDefault});
          if(current())drawState();
          await close();
        }catch(error){notify(error.message,true);}
        finally{saving=false;content.removeAttribute("aria-busy");}
      };
    }catch(error){notify(error.message,true);}
  };
  $("#conversation-settings",root).onclick=()=>app.openSettings(instance.agent_id);
  $("#instance-inspector",root).onclick=event=>{const expanded=$(".chat-layout",root).classList.toggle("show-inspector");event.currentTarget.setAttribute("aria-expanded",String(expanded));};
  jump.onclick=()=>{feed.scrollTop=feed.scrollHeight;jump.hidden=true;};
  feed.addEventListener("scroll",()=>{jump.hidden=nearBottom(feed);if(jump.hidden)markRead();},{passive:true});
  const resize=()=>{editor.style.height="auto";editor.style.height=Math.min(editor.scrollHeight,200)+"px";};
  editor.oninput=()=>{draft.text=editor.value;draft.key=crypto.randomUUID();for(const id of attachments.get(instance.id)?.keys()||[])if(!editor.value.includes(id))attachments.get(instance.id).delete(id);resize();$(".composer-send",root).disabled=submitting||!editor.value.trim()||!modelReady();};
  editor.onkeydown=event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229){event.preventDefault();if(!runs.some(run=>liveStates.has(run.state)))$("#composer",root).requestSubmit();}};
  $("#conversation-upload",root).onclick=()=>uploadArtifact(app,artifact=>{
    let selected=attachments.get(instance.id);if(!selected){selected=new Map();attachments.set(instance.id,selected);}
    if(selected.size>=32&&!selected.has(artifact.id))throw new Error("文件已上传；一次工作最多可分配 32 个来源文件。");
    selected.set(artifact.id,artifact);draft.text+="\nArtifactRef: "+artifact.id+" ("+artifact.name+")";draft.key=crypto.randomUUID();if(current()){editor.value=draft.text;resize();$(".composer-send",root).disabled=!modelReady();}
  });
  $("#conversation-stop",root).onclick=async event=>{const run=runs.find(value=>liveStates.has(value.state)),button=event.currentTarget;if(!run)return;button.disabled=true;try{await rpc("runs.cancel",{run_id:run.id});notify("停止请求已提交，实际结果以执行回执为准。");await refresh();}catch(error){button.disabled=false;notify(error.message,true);}};
  $("#composer",root).onsubmit=async event=>{
    if(removed){event.preventDefault();return;}
    event.preventDefault();const input=editor.value;if(submitting||!input.trim()||!modelReady()||runs.some(run=>liveStates.has(run.state)))return;
    const key=draft.key,artifact_ids=[...(attachments.get(instance.id)?.keys()||[])].filter(id=>input.includes(id));submitting=true;$(".composer-send",root).disabled=true;
    try{
      if(instance.draft){
        const oldId=instance.id,created=await rpc("instances.create",{session_id:sessionId,agent_id:instance.agent_id,name:instance.creationName,...(instance.model_ref?{model_ref:instance.model_ref}:{})});
        Object.assign(instance,created,{draft:false});drafts.delete(oldId);drafts.set(instance.id,draft);
        if(attachments.has(oldId)){attachments.set(instance.id,attachments.get(oldId));attachments.delete(oldId);}
        s.conversationDrafts=(s.conversationDrafts||[]).filter(value=>value!==instance);
        if(s.session?.id===sessionId){if(!s.instances.some(value=>value.id===instance.id))s.instances.push(instance);s.openTabs=s.openTabs.map(id=>id===oldId?instance.id:id);}
        if(active?.root===root)active.instanceId=instance.id;
        if(current()){$(".chat-identity h2",root).textContent=instance.name;app.refreshFrame();}
      }
      await rpc("runs.submit",{spec:{instance_id:instance.id,input,artifact_ids,idempotency_key:key}});if(draft.text===input){draft.text="";draft.key=crypto.randomUUID();}for(const id of artifact_ids)attachments.get(instance.id)?.delete(id);if(current()){editor.value=draft.text;resize();await refresh();feed.scrollTop=feed.scrollHeight;editor.focus();}await app.reloadInstances();app.refreshFrame();}
    catch(error){notify(error.message,true);}finally{submitting=false;if(current())$(".composer-send",root).disabled=!editor.value.trim()||!modelReady();}
  };
  resize();await refresh();
}
