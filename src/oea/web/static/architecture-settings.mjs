import { $,escape,icon,iconButton,signal,hint,element,modal,notify } from "./ui.mjs";
import { codeViewer } from "./code-viewer.mjs";

const candidates=new Map();
const names={react:"ReAct",plan_execute:"Plan & Execute",reflexion:"Reflexion"};
const stages={prepare:["准备","装配当前工作与获准上下文"],model:["决策","根据目标、上下文与证据，选择下一步行动"],tools:["工具","通过原有授权和持久回执执行工具"],deliver:["交付","保存完整结果，核验明确的交付要求"],subgraph:["子图","执行独立的阶段结构"]};
const titles={Prepare:"准备",Decide:"决策",Act:"工具",Deliver:"交付",Plan:"规划","Execute next step":"执行下一步",Evaluate:"评价",Reflect:"反思"};
const descriptions={Prepare:"装配当前工作与获准上下文。",Decide:"根据当前目标、上下文与证据，选择工具、继续推理或进入交付。",Act:"通过原有授权和持久回执执行工具，普通错误交回 Agent 排查。",Deliver:"保存完整结果，核验明确的交付要求。",Plan:"根据目标与已有证据，生成有界的顺序计划。","Execute next step":"继续计划中尚未完成的步骤，按证据决定是否重规划。",Evaluate:"根据目标和证据评价当前结果，给出具体反馈。",Reflect:"把本轮反馈转为下一次尝试可用的经验。"};
const defaultPreview={stages:["prepare","model","tools","deliver"].map(type=>({id:type,type,title:stages[type][0],description:stages[type][1]})),links:[{source:"prepare",target:"model"},{source:"model",target:"tools"},{source:"model",target:"deliver"},{source:"tools",target:"model"}],omitted:true,editable:false};

export function stagePreview(preview){
  const nodes=(preview.stages||[]).map(node=>({...node,description:descriptions[node.title]||node.description,title:titles[node.title]||node.title}));
  if(!nodes.length)return `<div class="architecture-empty">${icon("code")}<span>暂无预览</span>${hint("在源码中查看完整结构")}</div>`;
  const ids=nodes.map(node=>node.id),returns=(preview.links||[]).filter(edge=>ids.indexOf(edge.target)!==ids.indexOf(edge.source)+1);
  const returnPaths=[...new Set(returns.map(edge=>`${nodes.find(node=>node.id===edge.source)?.title||edge.source} → ${nodes.find(node=>node.id===edge.target)?.title||edge.target}`))].slice(0,4).join(" · ");
  return `<div class="architecture-stages" aria-label="架构关键阶段">${nodes.map((node,index)=>`<button type="button" class="architecture-stage ${index===0?"selected":""}" data-stage="${escape(node.id)}" aria-pressed="${index===0}" title="${escape(node.description||node.title)}"><span class="stage-symbol">${icon(({prepare:"file",model:"spark",tools:"terminal",deliver:"check",subgraph:"flow"})[node.type]||"flow")}</span><span>${escape(node.title||stages[node.type]?.[0]||node.id)}</span></button>${index<nodes.length-1?`<span class="architecture-arrow" aria-hidden="true">${(preview.links||[]).some(edge=>edge.source===node.id&&edge.target===nodes[index+1].id)?"→":"·"}</span>`:""}`).join("")}<span class="architecture-legend">${returns.length?`<span class="hint-icon" role="img" aria-label="${escape(returnPaths)}" title="${escape(returnPaths)}">${icon("branch")}</span>`:""}${preview.omitted?hint("关键阶段预览；部分路径已合并，完整结构见源码"):""}</span></div><span class="architecture-description sr-only" aria-live="polite">${escape(nodes[0].description||stages[nodes[0].type]?.[1]||nodes[0].title)}</span>`;
}

function wireStages(root,preview){
  for(const button of root.querySelectorAll("[data-stage]"))button.onclick=()=>{
    for(const sibling of root.querySelectorAll("[data-stage]")){const selected=sibling===button;sibling.classList.toggle("selected",selected);sibling.setAttribute("aria-pressed",String(selected));}
    const node=preview.stages.find(node=>node.id===button.dataset.stage);
    $(".architecture-description",root).textContent=descriptions[node.title]||node.description||stages[node.type]?.[1]||node.title;
  };
}

export async function renderArchitecture(app,root,draft){
  const load=Symbol();root.architectureLoad=load;
  const project_id=app.state.project.id,agent_id=draft.definition.id,key=project_id+":"+agent_id;
  const call=(action,params={})=>app.rpc("architectures."+action,{project_id,...params});
  const report=error=>{if(root.isConnected){const notice=$(".architecture-notice",root);if(notice){notice.textContent=error.message;notice.classList.add("v2-error");}else notify(error.message,true);}};
  root.innerHTML=`<div class="inline-status" role="status">${icon("refresh")}<span class="sr-only">正在读取架构状态</span></div>`;
  if(!draft.revision){root.innerHTML=`<section class="architecture-section"><h3>执行架构</h3><span class="muted-status">${signal("pending","尚未保存")}先保存 Agent</span></section>`;return;}
  try{
    const status=await call("status",{agent_id});
    const candidate=candidates.get(key)||status.binding?.architecture_id;
    const [active,editing]=await Promise.all([
      status.binding?call("preview",{architecture_id:status.binding.architecture_id,version:status.binding.version}):Promise.resolve(null),
      candidate?call("preview",{architecture_id:candidate}):Promise.resolve(null)
    ]);
    if(!root.isConnected||root.architectureLoad!==load)return;
    draft.workStatus=status.blocker?.source==="application"?"应用工作未结束或待核对":status.blocker?({running:"执行中",accepted:"已接受工作",waiting:"等待输入或依赖",needs_review:"结果待核对"}[status.blocker.state]||"有待核对工作"):"可接受新工作";
    const workStatus=root.closest("form")?.querySelector("[data-work-status]");if(workStatus)workStatus.innerHTML=signal(status.blocker?.source==="application"?"waiting":status.blocker?.state||"idle",draft.workStatus);
    const title=active?`${active.name} · v${active.version}`:status.program?"进程 Agent":status.graph==="default"?"默认 ReAct":`${status.graph} · ${status.graph_version}`;
    const preview=active||(!status.program&&status.graph==="default"?defaultPreview:{stages:[],omitted:true});
    root.innerHTML=`<section class="architecture-section">
      <header class="architecture-heading"><h3>执行架构 <span class="architecture-version">${signal("online","当前生效架构")}${escape(title)}</span></h3>${iconButton("code","编辑源码",{"data-source":true})}</header>
      <div class="architecture-preview">${stagePreview(preview)}</div>
      ${status.blocker?`<div class="architecture-blocker">${signal("waiting","切换受阻")}<span>${status.blocker.source==="application"?"请从所属应用完成或核对原工作":"先完成或核对原工作"}</span>${status.blocker.run_id?iconButton("clock","查看阻塞工作",{"data-blocker":true}):""}</div>`:""}
      <div class="architecture-candidate"><div><h4>候选草稿</h4><span class="v2-muted">${editing?`${escape(editing.name)} · r${editing.revision}`:"未创建"}</span></div><div class="v2-actions">${editing?iconButton("shield","校验",{"data-validate":true})+iconButton("upload","发布候选",{"data-publish":true}):""}${iconButton("branch","切换版本",{"data-versions":true})}</div></div>
      ${editing?`<div class="architecture-draft-actions">${iconButton("code","编辑草稿源码",{"data-draft-source":true})}${editing.editable?iconButton("sliders","调整选项",{"data-adjust":true}):hint("代码图仅支持在源码中修改")}</div>`:""}
      <div class="architecture-template"><label for="architecture-template">模板</label><select id="architecture-template">${Object.entries(names).map(([name,title])=>`<option value="${name}">${title}</option>`).join("")}</select>${iconButton("plus","创建草稿",{"data-create":true})}</div>
      <div class="architecture-notice" role="status"></div></section>`;
    wireStages($(".architecture-preview",root),preview);
    const refresh=()=>renderArchitecture(app,root,draft);
    async function source(identity,version){
      if(!identity){const reference=status.program?{program:draft.definition.execution.program}:{graph:status.graph,graph_version:status.graph_version};modal("执行入口",codeViewer(JSON.stringify(reference,null,2),{filename:"execution.json",language:"json"}),{size:"wide"});return;}
      const exported=await call("export",{architecture_id:identity,...(version?{version}:{})});
      const text=typeof exported.source==="string"?exported.source:JSON.stringify(exported.source,null,2);
      modal("编辑架构源码",codeViewer(text,{filename:exported.filename||"source.json",language:exported.filename?"python":"json",download:!!exported.filename}),{size:"wide"});
    }
    $("[data-source]",root).onclick=()=>source(status.binding?.architecture_id,status.binding?.version).catch(report);
    if(editing)$("[data-draft-source]",root).onclick=()=>source(editing.id).catch(report);
    if(editing){const button=element(iconButton("eye","预览草稿"));$(".architecture-draft-actions",root).prepend(button);button.onclick=()=>{const content=element(`<div>${stagePreview(editing)}</div>`);modal("候选草稿预览",content,{size:"wide"});wireStages(content,editing);};}
    if(status.blocker?.run_id)$("[data-blocker]",root).onclick=()=>app.showRun(status.blocker.run_id);
    $("[data-create]",root).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{
      const name=$("select",root).value,template=await app.rpc("architectures.templates",{name}),id="architecture-"+crypto.randomUUID();
      await app.rpc("architectures.create",{definition:{id,project_id,name:names[name]},draft:template.graph});candidates.set(key,id);await refresh();notify("草稿已创建");
    }catch(error){report(error);button.disabled=false;}};
    if(editing){
      $("[data-validate]",root).onclick=async()=>{try{const result=await call("validate",{architecture_id:editing.id,expected_revision:editing.revision});$(".architecture-notice",root).textContent=result.valid?"校验通过":result.errors.map(error=>`${error.path.join(".")}：${error.message}`).join("\n");}catch(error){report(error);}};
      $("[data-publish]",root).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{const result=await call("publish",{architecture_id:editing.id,expected_revision:editing.revision});await refresh();const notice=$(".architecture-notice",root);if(notice)notice.textContent=`候选 v${result.version} 已发布`;}catch(error){report(error);button.disabled=false;}};
      const adjust=$("[data-adjust]",root);
      if(adjust)adjust.onclick=()=>{
        const content=element(`<form><div class="field"><label for="architecture-max-steps">图步数上限</label><input id="architecture-max-steps" type="number" min="1" max="100000" required value="${escape(editing.options?.max_steps||512)}"></div>${editing.options?.max_attempts?`<div class="field"><label for="architecture-max-attempts">尝试次数上限</label><input id="architecture-max-attempts" type="number" min="1" max="10" required value="${escape(editing.options.max_attempts)}"></div>`:""}<p class="architecture-notice" role="status"></p><footer class="dialog-footer">${iconButton("check","保存草稿选项",{type:"submit",className:"primary"})}</footer></form>`),close=modal("调整草稿选项",content);
        content.onsubmit=async event=>{event.preventDefault();try{const attempts=$("#architecture-max-attempts",content),options={max_steps:Number($("#architecture-max-steps",content).value),...(attempts?{max_attempts:Number(attempts.value)}:{})};await call("adjust",{architecture_id:editing.id,expected_revision:editing.revision,options});close();await refresh();}catch(error){$(".architecture-notice",content).textContent=error.message;}};
      };
    }
    $("[data-versions]",root).onclick=()=>versions().catch(report);
    async function versions(){
      let page=await call("list",{limit:25}),items=page.items;
      const content=element(`<form class="architecture-version-form"><div class="field"><label for="version-architecture">架构</label><div class="input-actions"><select id="version-architecture"></select>${iconButton("plus","加载更多架构",{"data-more":true})}</div></div><div class="field"><label for="architecture-version">已发布版本</label><div class="input-actions"><select id="architecture-version"></select>${iconButton("plus","加载更多版本",{"data-more-versions":true})}</div></div><p class="architecture-notice" role="status"></p><footer class="dialog-footer">${iconButton("check","应用到当前 Agent",{type:"submit",className:"primary",disabled:true})}</footer></form>`),close=modal("切换生效版本",content);
      content.onsubmit=event=>event.preventDefault();
      let versionPage;
      const architectures=$("#version-architecture",content),versions=$("#architecture-version",content),submit=$("button[type=submit]",content);
      function choices(){architectures.innerHTML=items.map(item=>`<option value="${escape(item.id)}">${escape(item.name)}</option>`).join("");$("[data-more]",content).hidden=!page.cursor;}
      choices();if(items.some(item=>item.id===candidate))architectures.value=candidate;
      const blocked=status.blocker||draft.dirty;
      $(".architecture-notice",content).textContent=status.blocker?"原工作未完成或待核对，暂不能切换":draft.dirty?"请先保存 Agent 设置":"";
      async function loadVersions(append=false){
        versionPage=architectures.value?await call("versions",{architecture_id:architectures.value,limit:25,...(append?{cursor:versionPage.cursor}:{})}):{items:[],cursor:null};
        if(!append)versions.replaceChildren();for(const item of versionPage.items){const option=document.createElement("option");option.value=item.version;option.textContent=`v${item.version} · 草稿修订 ${item.draft_revision}`;versions.append(option);}
        if(!append&&versionPage.items.length)versions.value=versionPage.items.at(-1).version;
        $("[data-more-versions]",content).hidden=!versionPage.cursor;submit.disabled=!!blocked||!versions.value;
      }
      architectures.onchange=()=>loadVersions().catch(error=>$(".architecture-notice",content).textContent=error.message);
      $("[data-more]",content).onclick=async()=>{try{page=await call("list",{cursor:page.cursor,limit:25});const old=architectures.value;items.push(...page.items);choices();architectures.value=old;}catch(error){$(".architecture-notice",content).textContent=error.message;}};
      $("[data-more-versions]",content).onclick=()=>loadVersions(true).catch(error=>$(".architecture-notice",content).textContent=error.message);
      await loadVersions();
      content.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{
        const saved=await call("bind",{agent_id,binding:{architecture_id:architectures.value,version:Number(versions.value)},expected_revision:draft.revision});
        draft.definition=saved.definition;draft.revision=saved.revision;candidates.set(key,architectures.value);close();await app.reloadAgents();await refresh();notify("架构版本已生效。");
      }catch(error){$(".architecture-notice",content).textContent=error.message;submit.disabled=false;}};
    }
  }catch(error){if(!root.isConnected||root.architectureLoad!==load)return;root.innerHTML=`<section class="architecture-section"><h3>执行架构</h3><p class="v2-error">${escape(error.message)}</p>${iconButton("refresh","重新读取",{"data-retry":true})}</section>`;$("[data-retry]",root).onclick=()=>renderArchitecture(app,root,draft);}
}
