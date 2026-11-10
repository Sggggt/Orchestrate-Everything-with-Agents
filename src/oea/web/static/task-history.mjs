import { $,escape,icon,rpc,element,modal,notify,badge } from "./ui.mjs";
import { compactTitle } from "./presentation.mjs";
import { showRun,taskBadge } from "./runs.mjs";
const historyTimestamp=new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});

export async function showTaskHistory(app,view="all"){
  const project_id=app.state.project?.id,currentSession=app.state.session?.id;
  if(!project_id)return;
  const attention=view==="attention",selected=new Map();
  let scope="project",page=0,cursors=[0],next=null,items=[],busy=false,closed=false,epoch=0,timer=null,refreshOverview=false;
  const content=element(`<section class="task-history-dialog"><div class="task-history-toolbar"><label>范围<select data-history-scope><option value="project">当前项目 · 全部会话（含已归档）</option>${currentSession?'<option value="session">当前会话</option>':""}</select></label><div class="task-history-actions">${attention?'<span data-selection-count>已选 0 项</span><button class="btn small" data-handle disabled>标记已处理</button>':""}<button class="icon-btn" data-refresh title="刷新记录" aria-label="刷新记录">${icon("refresh")}</button></div></div>${attention?'<p class="task-history-hint">标记为已处理后从待办移除，原执行状态、回执和用量记录继续保留。</p>':""}<div class="task-history-table" data-rows></div><p class="task-history-error" role="status" data-notice></p><footer class="task-history-footer"><span data-page-label></span><div><button class="icon-btn" data-previous title="上一页" aria-label="上一页">${icon("chevron")}</button><button class="icon-btn" data-next title="下一页" aria-label="下一页">${icon("chevron")}</button></div></footer></section>`);
  const close=modal(attention?"所有待处理任务":"所有历史任务",content,{size:"wide"});
  function controls(){
    content.setAttribute("aria-busy",String(busy));
    $("[data-previous]",content).disabled=busy||page===0;$("[data-next]",content).disabled=busy||next==null;
    $("[data-refresh]",content).disabled=busy;$("[data-history-scope]",content).disabled=busy;
    if(attention){$("[data-handle]",content).disabled=busy||selected.size===0;$("[data-selection-count]",content).textContent=`已选 ${selected.size} 项`;}
    for(const check of content.querySelectorAll('[data-select-task],[data-select-page]'))check.disabled=busy;
  }
  function render(){
    $("[data-rows]",content).innerHTML=items.length?`<table><thead><tr>${attention?'<th class="task-checkbox"><input type="checkbox" data-select-page aria-label="选择本页任务"></th>':""}<th>任务</th><th class="task-scope-column">会话 / Agent</th><th class="task-state-column">状态</th><th class="task-time-column">创建时间</th><th><span class="sr-only">详情</span></th></tr></thead><tbody>${items.map(run=>{
      const title=compactTitle(run.display?.title||run.goal||run.instance_name||"工作任务");
      return `<tr>${attention?`<td class="task-checkbox"><input type="checkbox" data-select-task="${escape(run.id)}" aria-label="选择 ${escape(title)}" ${selected.has(run.id)?"checked":""}></td>`:""}<td><button class="text-link task-history-title" data-open-task="${escape(run.id)}" title="${escape(title)}">${escape(title)}</button><small>${escape(run.instance_name)}</small></td><td><span>${escape(run.session_name)}${run.session_archived?" · 已归档":""}</span><small>${escape(app.state.agents.find(item=>item.definition.id===run.agent_id)?.definition.name||run.agent_id)}</small></td><td>${taskBadge(run)}${run.handling_state==="handled"?`<small>执行状态：${badge(run.state)}</small>`:run.attention_reasons?.includes("usage")?'<small>用量待核对</small>':""}</td><td class="task-history-time">${historyTimestamp.format(run.created_at)}</td><td><button class="icon-btn" data-open-task="${escape(run.id)}" title="查看任务详情" aria-label="查看 ${escape(title)} 的详情">${icon("arrow")}</button></td></tr>`;
    }).join("")}</tbody></table>`:`<p class="quiet-state">${attention?"此范围没有待处理任务。":"此范围没有历史任务。"}</p>`;
    $("[data-page-label]",content).textContent=`第 ${page+1} 页 · 每页 20 条${items.length?` · 本页 ${items.length} 条`:""}`;
    for(const button of content.querySelectorAll('[data-open-task]'))button.onclick=()=>showRun(app,button.dataset.openTask,items.find(run=>run.id===button.dataset.openTask));
    for(const check of content.querySelectorAll('[data-select-task]'))check.onchange=()=>{
      const row=items.find(item=>item.id===check.dataset.selectTask);
      if(check.checked){if(selected.size>=100){check.checked=false;notify("每次最多选择 100 项，请先处理已有选择",true);return;}selected.set(row.id,{run_id:row.id,expected_updated_at:row.updated_at});}
      else selected.delete(row.id);
      selectionState();
    };
    const all=$("[data-select-page]",content);if(all)all.onchange=()=>{
      if(all.checked){const additions=items.filter(row=>!selected.has(row.id));if(selected.size+additions.length>100){all.checked=false;notify("每次最多选择 100 项，请先处理已有选择",true);return;}for(const row of additions)selected.set(row.id,{run_id:row.id,expected_updated_at:row.updated_at});}
      else for(const row of items)selected.delete(row.id);
      for(const check of content.querySelectorAll('[data-select-task]'))check.checked=selected.has(check.dataset.selectTask);
      selectionState();
    };
    selectionState();
  }
  function selectionState(){const all=$("[data-select-page]",content);if(all){const count=items.filter(row=>selected.has(row.id)).length;all.checked=count===items.length&&count>0;all.indeterminate=count>0&&count<items.length;}controls();}
  async function load({reset=false}={}){
    if(closed)return;if(reset){page=0;cursors=[0];}
    const request=++epoch;busy=true;controls();
    try{
      const result=await rpc("runs.list",{project_id,view,limit:20,cursor:cursors[page],...(scope==="session"?{session_id:currentSession}:{})});
      if(closed||request!==epoch)return;
      items=result.items;next=result.cursor;
      for(const row of items)if(selected.has(row.id)&&(row.handling_state==="handled"||selected.get(row.id).expected_updated_at!==row.updated_at))selected.delete(row.id);
      render();
    }catch(error){if(!closed)$("[data-notice]",content).textContent=error.message;}
    finally{if(!closed&&request===epoch){busy=false;controls();}}
  }
  $("[data-history-scope]",content).onchange=event=>{scope=event.target.value;selected.clear();$("[data-notice]",content).textContent="";load({reset:true});};
  $("[data-previous]",content).onclick=()=>{if(page>0){page--;load();}};
  $("[data-next]",content).onclick=()=>{if(next!=null){page++;cursors[page]=next;load();}};
  $("[data-refresh]",content).onclick=()=>load();
  if(attention)$("[data-handle]",content).onclick=async()=>{
    busy=true;controls();const request=[...selected.values()];
    try{
      const result=await rpc("runs.mark_handled",{project_id,items:request});
      if(closed)return;
      for(const item of [...result.items,...result.conflicts])selected.delete(item.run_id);
      $("[data-notice]",content).textContent=result.conflicts.length?`${result.items.length} 项已处理；${result.conflicts.length} 项状态已变化，请重新选择。`:`已将 ${result.items.length} 项标记为已处理。`;
      await load({reset:true});await app.reloadInstances();if(app.state.page==="overview")await app.render();refreshOverview=false;
    }catch(error){if(!closed){$("[data-notice]",content).textContent=`未确认标记结果：${error.message}。已重新读取记录。`;await load({reset:true});}}
    finally{if(!closed){busy=false;controls();}}
  };
  const listener=fact=>{
    if(closed||fact.project_id!==project_id||!(/^(run\.|usage\.corrected)/.test(fact.kind)))return;
    refreshOverview=true;
    if(timer)clearTimeout(timer);timer=setTimeout(()=>{timer=null;if(!busy)load();},120);
  };
  app.runListeners.add(listener);
  close.onclose=()=>{closed=true;epoch++;clearTimeout(timer);app.runListeners.delete(listener);if(app.state.page==="overview")(refreshOverview?app.reloadInstances():Promise.resolve()).then(()=>{if(app.state.page==="overview")return app.render();}).catch(error=>notify(error.message,true));};
  await load();
}
