import { $,escape,icon,element,modal,notify,confirmRemoval } from "./ui.mjs";
import { thumbnail } from "./agent-graph.mjs";
const selections=new Map(),names={react:"ReAct",plan_execute:"Plan & Execute",reflexion:"Reflexion"};
const key=(app,agent)=>app.state.project.id+":"+agent;
export function selectArchitecture(app,agent,id){selections.set(key(app,agent),id?{id}:null);}
export const stagePreview=value=>thumbnail(value.canvas||{nodes:[],edges:[]},escape);
export const architectureCall=(app,project_id)=>(action,params={})=>app.rpc("architectures."+action,{...(action==="create"?{}:{project_id}),...params});
export function acceptBinding(draft,saved){
  if(draft.dirty){
    const execution=saved.definition.execution;
    Object.assign(draft.definition.execution,{architecture:execution.architecture,graph:execution.graph,graph_version:execution.graph_version,program:execution.program});
    // A recovered response may already include another writer's later revision.
    // Keep the original CAS boundary rather than overwriting those unseen fields.
    if(saved.revision!==draft.revision+1){draft.bindingConflict=true;return;}
  }
  else draft.definition=saved.definition;
  draft.revision=saved.revision;draft.bindingConflict=false;
}

export async function renderArchitecture(app,root,agentDraft){
  const epoch=Symbol();root.loadEpoch=epoch;
  const project_id=app.state.project.id,agent_id=agentDraft.definition.id;
  const call=architectureCall(app,project_id);
  const current=()=>root.isConnected&&root.loadEpoch===epoch,report=error=>{if(current())notify(error.message,true);};
  if(!agentDraft.revision){root.innerHTML='<div class="ag-empty">保存 Agent 后即可编排节点画布。</div>';return;}
  root.innerHTML='<div class="ag-loading" role="status">正在读取节点画布…</div>';
  try{
    const [status,page]=await Promise.all([call("status",{agent_id,canvas:true}),call("list",{owner_agent_id:agent_id,limit:25})]);
    let selected=selections.get(key(app,agent_id));
    const active=status.binding?await call("preview",{...status.binding,detail:"canvas"}):{name:status.program?"进程 Agent":status.graph==="default"?"默认 ReAct":status.graph,canvas:status.canvas};
    let view=selected?await call("preview",{architecture_id:selected.id,...(selected.version?{version:selected.version}:{}),detail:"canvas"}):active;
    if(view.removed&&!selected?.version){selected=null;selections.delete(key(app,agent_id));view=active;}
    if(!current())return;
    const activeLabel=status.binding?`${active.name} · v${active.version}`:active.name;
    const viewed=selected?(selected.version?`${view.name} · v${view.version}`:`${view.name} · 草稿 r${view.revision}`):activeLabel;
    const same=!!status.binding&&view.id===status.binding.architecture_id&&(selected?.version?selected.version===status.binding.version:view.revision===active.revision);
    const editing=selected&&!selected.version&&!view.removed;
    root.innerHTML=`<section class="ag-settings">
      <div class="ag-version-heading"><div><span class="ag-eyebrow">当前生效</span><strong>${escape(activeLabel)}</strong>${status.binding?`<span class="ag-muted">来自草稿 r${active.revision}</span>`:""}</div><button type="button" class="btn small" data-history>${icon("clock")}历史版本</button></div>
      <button type="button" class="ag-preview-entry" aria-label="进入 Agent 节点画布工作台" title="点击进入画布工作台">${view.canvas?.nodes?.length?thumbnail(view.canvas,escape):`<span class="ag-empty">${icon("code")}${status.program?"进程 Agent 使用已配置的程序入口":"此代码图暂无已注册拓扑"}</span>`}<span class="ag-preview-caption"><span>${icon("flow")} ${escape(viewed)}</span><span>${icon("external")}进入画布</span></span>${view.canvas?.omitted?'<span class="ag-preview-note">动态路径可能未完整展示</span>':""}</button>
      <div class="ag-candidate-heading"><div><h3>候选草稿</h3><span class="ag-muted">选择草稿查看，确认切换后生效</span></div><button class="btn small" type="button" data-new>${icon("plus")}新建草稿</button></div>
      <div class="ag-candidate-controls"><select aria-label="选择候选草稿" data-select><option value="">当前生效 · ${escape(activeLabel)}</option>${page.items.map(item=>`<option value="${escape(item.id)}" ${editing&&item.id===selected.id?"selected":""}>${escape(item.name)} · r${item.revision}</option>`).join("")}</select>${page.cursor!=null?'<button class="btn small" type="button" data-more>更多草稿</button>':""}<button class="btn small" type="button" data-copy title="复制正在查看的架构">${icon("branch")}复制</button>${editing?`<button class="icon-btn" type="button" data-rename title="重命名草稿" aria-label="重命名草稿">${icon("file")}</button><button class="icon-btn danger-text" type="button" data-remove title="删除草稿" aria-label="删除草稿">${icon("trash")}</button>`:""}</div>
      <div class="ag-switch-row"><span class="ag-muted">正在查看：${escape(viewed)}</span><button class="btn primary" type="button" data-activate ${!selected||same?"disabled":""}>${icon("check")}切换版本</button></div>
      <p class="ag-muted">切换只影响之后新接受的任务，已有任务继续使用原版本。</p>
    </section>`;
    if(editing&&!page.items.some(item=>item.id===selected.id))$("[data-select]",root).add(new Option(`${view.name} · r${view.revision}`,selected.id,true,true));
    if(selected?.version){const option=new Option(`正在查看已发布版本：${viewed}`,"@version",true,true);option.disabled=true;$("[data-select]",root).add(option);}
    const refresh=()=>renderArchitecture(app,root,agentDraft);
    $("[data-select]",root).onchange=event=>{selectArchitecture(app,agent_id,event.target.value);refresh();};
    $(".ag-preview-entry",root).onclick=()=>app.openAgentCanvas({agent_id,architecture_id:selected?.id||status.binding?.architecture_id||null,version:selected?selected.version||null:status.binding?.version||null});
    async function create(name,graph,representation="declarative",source={}){const id="architecture-"+crypto.randomUUID();await call("create",{definition:{id,project_id,owner_agent_id:agent_id,name,representation,source},draft:graph});selectArchitecture(app,agent_id,id);await refresh();notify("候选草稿已创建，当前版本未改变");}
    async function copyViewed(){const identity=selected?.id||status.binding?.architecture_id,version=selected?selected.version:status.binding?.version;if(identity){const value=await call("get",{architecture_id:identity,...(version?{version}:{})});await create(value.name+" 副本",value.definition||value.draft,value.representation,{...value.source,copied_from:{architecture_id:identity,version:version||null,revision:value.revision}});}else if(status.graph==="default"&&!status.program)await create("ReAct 草稿",(await app.rpc("architectures.templates",{name:"react"})).graph);else notify("请从模板新建草稿，代码入口不会被逆向改写",true);}
    $("[data-copy]",root).onclick=()=>copyViewed().catch(report);
    $("[data-new]",root).onclick=()=>{
      const form=element(`<form><label class="field">草稿名称<input name="name" required maxlength="120" value="新架构"></label><label class="field">开始方式<select name="template">${Object.entries(names).map(([id,title])=>`<option value="${id}">${title}</option>`).join("")}<option value="copy">从已有架构复制</option></select></label><div data-library hidden></div><p data-error class="v2-error"></p><footer class="dialog-footer"><button class="btn primary" type="submit">创建草稿</button></footer></form>`),close=modal("新建候选草稿",form);let library={items:[],cursor:null};
      async function load(append=false){const next=await call("list",{limit:25,...(append?{cursor:library.cursor}:{})});library={items:append?[...library.items,...next.items]:next.items,cursor:next.cursor};const area=$("[data-library]",form);area.hidden=false;area.innerHTML=`<label class="field">已有架构<select name="source">${library.items.map(item=>`<option value="${escape(item.id)}">${escape(item.name)}</option>`).join("")}</select></label>${library.cursor!=null?'<button type="button" class="btn small" data-next>加载更多</button>':""}`;const more=$("[data-next]",area);if(more)more.onclick=()=>load(true).catch(error=>$("[data-error]",form).textContent=error.message);}
      form.elements.template.onchange=()=>{if(form.elements.template.value==="copy")load().catch(error=>$("[data-error]",form).textContent=error.message);else $("[data-library]",form).hidden=true;};
      form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{const mode=form.elements.template.value;if(mode==="copy"){const value=await call("get",{architecture_id:form.elements.source.value});await create(form.elements.name.value,value.draft,value.representation,{...value.source,copied_from:{architecture_id:value.id,revision:value.revision}});}else await create(form.elements.name.value,(await app.rpc("architectures.templates",{name:mode})).graph);close();}catch(error){$("[data-error]",form).textContent=error.message;button.disabled=false;}};
    };
    const more=$("[data-more]",root);if(more)more.onclick=async()=>{try{const next=await call("list",{owner_agent_id:agent_id,cursor:page.cursor,limit:25});for(const item of next.items)if(![...$("[data-select]",root).options].some(o=>o.value===item.id))$("[data-select]",root).add(new Option(`${item.name} · r${item.revision}`,item.id));page.cursor=next.cursor;more.hidden=next.cursor==null;}catch(error){report(error);}};
    if(editing){
      $("[data-remove]",root).onclick=()=>confirmRemoval("删除候选草稿",`删除“${view.name}”的候选草稿？已发布版本、当前生效版本和历史运行会保留。`,async()=>{await call("remove",{architecture_id:view.id,owner_agent_id:agent_id,expected_revision:view.revision});selectArchitecture(app,agent_id,null);await refresh();});
      $("[data-rename]",root).onclick=()=>{const form=element(`<form><label class="field">草稿名称<input required maxlength="120" value="${escape(view.name)}"></label><p class="v2-error"></p><footer class="dialog-footer"><button class="btn primary" type="submit">保存名称</button></footer></form>`),close=modal("重命名草稿",form);form.onsubmit=async event=>{event.preventDefault();try{const value=await call("get",{architecture_id:view.id});await call("save",{architecture_id:view.id,expected_revision:view.revision,draft:value.draft,name:$("input",form).value});close();await refresh();}catch(error){$("p",form).textContent=error.message;}};};
    }
    $("[data-activate]",root).onclick=()=>{
      const form=element(`<form><p>将当前 Agent 从 <strong>${escape(activeLabel)}</strong> 切换为 <strong>${escape(viewed)}</strong>？</p><p class="ag-muted">确认后校验并固定版本。已有任务沿用原图，之后新接受的任务使用新版本。</p><p class="v2-error" data-error></p><footer class="dialog-footer"><button class="btn" type="button" data-cancel>取消</button><button class="btn primary" type="submit">确认切换</button></footer></form>`),close=modal("切换生效版本",form);$("[data-cancel]",form).onclick=close;
      form.onsubmit=async event=>{event.preventDefault();form.querySelector('[type=submit]').disabled=true;let published=null;try{published=selected.version?{version:selected.version}:await call("publish",{architecture_id:view.id,expected_revision:view.revision});const saved=await call("bind",{agent_id,binding:{architecture_id:view.id,version:published.version},expected_revision:agentDraft.revision});acceptBinding(agentDraft,saved);selectArchitecture(app,agent_id,null);await app.reloadAgents();close();await refresh();notify("版本已切换");}catch(error){let applied=false;if(published){try{const actual=await call("status",{agent_id});applied=actual.binding?.architecture_id===view.id&&actual.binding.version===published.version;if(applied){const saved=await app.rpc("agents.get",{agent_id});acceptBinding(agentDraft,saved);}}catch{/* Reconcile before any repeat. */}}if(applied){close();selectArchitecture(app,agent_id,null);await refresh();notify("已核实版本生效");}else $("[data-error]",form).textContent=(published?"版本已生成，尚未确认生效。":"未切换版本。")+error.message+" 关闭后重新读取状态再操作。";}};
    };
    $("[data-history]",root).onclick=async()=>{try{const identity=selected?.id||status.binding?.architecture_id;if(!identity){notify("当前使用内置程序，尚无发布版本");return;}const content=element('<div><div data-versions></div><button class="btn small" data-next>加载更多</button></div>'),close=modal("选择已发布版本",content);let cursor=0;async function load(){const page=await call("versions",{architecture_id:identity,cursor,limit:25});for(const item of page.items){const button=element(`<button class="ag-version-option" type="button">v${item.version}<span>来自草稿 r${item.draft_revision}</span>${status.binding?.architecture_id===identity&&status.binding.version===item.version?'<span class="ag-pill">当前生效</span>':""}</button>`);button.onclick=()=>{selections.set(key(app,agent_id),{id:identity,version:item.version});close();refresh();};$("[data-versions]",content).append(button);}cursor=page.cursor;$("[data-next]",content).hidden=cursor==null;}$("[data-next]",content).onclick=()=>load().catch(report);await load();}catch(error){report(error);}};
  }catch(error){if(current()){root.innerHTML=`<div class="ag-empty">${escape(error.message)}<button class="btn" type="button">重新读取</button></div>`;$("button",root).onclick=()=>{selections.delete(key(app,agent_id));renderArchitecture(app,root,agentDraft);};}}
}
