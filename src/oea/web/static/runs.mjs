import { $,escape,icon,rpc,element,modal,notify,empty,badge,date } from "./ui.mjs";
import { markdown } from "./markdown.mjs";
import { usageRows,projectionRows,cancellationHTML,diagnosisHTML,integrityLabel } from "./diagnostics.mjs";
import { compactTitle } from "./presentation.mjs";

export async function renderRuns(app,root){
  const s=app.state;if(!s.session){root.innerHTML=empty("还没有工作会话","先创建会话，再开始一个明确的工作目标。",'<button class="btn primary" id="create-session">新建会话</button>');$("#create-session").onclick=app.createSession;return;}
  const page=await rpc("runs.list",{session_id:s.session.id,project_id:s.project.id,limit:25});if(s.page!=="runs")return;
  root.className="v2-content";root.innerHTML=`<div class="v2-heading"><div><h1>运行记录</h1><p>读取原回执、核对成果，在原实例中继续工作。</p></div><button class="btn primary" id="new-run">${icon("plus")} 新工作</button></div><div class="v2-run-list">${page.items.map(run=>`<article class="v2-run-card"><div><h3>${escape(run.display?.title||run.instance_name)} <small class="v2-muted">${escape(run.agent_id)} · ${run.model_role==="manager"?"manager":"实例"}</small></h3><p>${escape(run.goal||"明确工作意图")}</p><p class="v2-muted">${date(run.created_at)}</p></div><div class="v2-actions">${badge(run.state)}<button class="btn" data-run="${escape(run.id)}">查看工作</button></div></article>`).join("")||empty("开始第一个工作","给 Agent 一个明确目标，交付和错误会保留在这里。")}</div>`;
  $("#new-run").onclick=()=>newRun(app);for(const button of root.querySelectorAll("[data-run]"))button.onclick=()=>showRun(app,button.dataset.run);
}
export async function showRun(app,identity){
  const content=element('<div class="run-dialog"><div id="run-observation">正在读取任务…</div><footer class="dialog-footer"><button class="btn" id="run-files">文件候选</button><button class="icon-btn" id="refresh-run" title="刷新任务" aria-label="刷新任务">'+icon("refresh")+'</button><button class="icon-btn" id="open-run-chat" title="打开对话" aria-label="打开对话">'+icon("chat")+'</button></footer></div>');
  const close=modal("任务详情",content,{size:"medium"});let closed=false,loading=false,run=null,timer=null;
  const listener=fact=>{if(fact.run_id===identity&&!timer)timer=setTimeout(()=>{timer=null;render().catch(error=>notify(error.message,true));},100);};
  app.runListeners.add(listener);close.onclose=()=>{closed=true;clearTimeout(timer);app.runListeners.delete(listener);};
  async function render(){
    if(closed||loading)return;loading=true;
    try{run=await rpc("runs.inspect",{run_id:identity});if(closed)return;
      const summary=app.state.runs.find(value=>value.id===identity);
      $("#run-observation",content).innerHTML=`<div class="run-heading"><h3>${escape(compactTitle(run.display?.title||summary?.goal||"工作任务"))}</h3>${badge(run.state)}</div><dl class="v2-kv"><dt>实例</dt><dd>${escape(summary?.instance_name||run.instance_id)}</dd><dt>创建</dt><dd>${date(run.created_at)}</dd><dt>更新</dt><dd>${date(run.updated_at||run.created_at)}</dd>${usageRows(run.usage.run_direct)}${projectionRows(run.model_input)}${run.display?.item_label?`<dt>条目</dt><dd>${escape(run.display.item_label)}</dd>`:""}<dt>任务标识</dt><dd>${escape(run.id)}</dd></dl><details><summary>根工作及共享预算用量</summary><dl class="v2-kv">${usageRows(run.usage.root_work)}${usageRows(run.usage.shared_budget)}</dl></details>${cancellationHTML(run)}${diagnosisHTML(run.error)}${run.activity?.blocker?`<details><summary>最近阻断原因 · 重复 ${run.activity.blocker_repeats} 次</summary>${diagnosisHTML(run.activity.blocker)}</details>`:""}${run.wait?`<p class="run-wait">${escape(run.wait.prompt||(run.wait.kind==="context"?(run.wait.reason?.safe_message||"等待上下文恢复处理"):"等待子工作回执"))}</p>`:""}${["failed","waiting"].includes(run.state)&&!run.cancel_requested_at?'<button class="btn" id="resume-run">继续原工作</button>':""}`;
      if(run.actions.length)$("#run-observation",content).insertAdjacentHTML("beforeend",`<details><summary>动作与命令回执</summary>${run.actions.map(action=>`<section class="v2-panel"><p>${escape(action.name)} ${action.result_is_error?badge("failed"):badge(action.state,true)}${action.exit_code==null?"":` · 退出码 ${action.exit_code}`}</p><div class="inline-actions"><button class="btn btn-small" data-action-read="${escape(action.id)}">原回执</button>${action.command_session_id?`<button class="btn btn-small" data-command="${escape(action.command_session_id)}">命令输出</button>`:""}${action.archived_count?`<button class="btn btn-small" data-attempts="${escape(action.id)}">历史尝试</button>`:""}${action.state==="needs_review"?`<button class="btn btn-small" data-confirm="${escape(action.id)}">核对原动作</button>`:""}</div></section>`).join("")}</details>`);
      const resume=$("#resume-run",content);if(resume)resume.onclick=()=>resumeRun(run,render);
      for(const button of content.querySelectorAll("[data-evidence-action]"))button.onclick=()=>showAction(button.dataset.evidenceAction,button.dataset.evidenceAttempt||null);
      for(const button of content.querySelectorAll("[data-action-read]"))button.onclick=()=>showAction(button.dataset.actionRead);
      for(const button of content.querySelectorAll("[data-command]"))button.onclick=()=>showCommand(app,run.id,button.dataset.command);
      for(const button of content.querySelectorAll("[data-attempts]"))button.onclick=()=>showAttempts(app,button.dataset.attempts);
      for(const button of content.querySelectorAll("[data-confirm]"))button.onclick=()=>confirmAction(app,run.actions.find(action=>action.id===button.dataset.confirm),render);
    }finally{loading=false;}
  }
  $("#run-files",content).onclick=()=>{if(run)showCandidates(app,run.id);};
  $("#refresh-run",content).onclick=()=>render().catch(error=>notify(error.message,true));
  $("#open-run-chat",content).onclick=async()=>{if(!run)return;try{close();await app.openInstance(run.instance_id);}catch(error){notify(error.message,true);}};
  try{await render();}catch(error){if(!closed)$("#run-observation",content).textContent=error.message;notify(error.message,true);}
}
export async function showCommand(app,runId,identity){
  const content=element('<div><p id="command-status"></p><dl class="v2-kv" id="command-environment"></dl><p class="v2-muted" id="command-capture"></p><div id="command-downloads" class="inline-actions"></div><h3>标准输出</h3><pre id="command-stdout" class="v2-pre"></pre><h3>标准错误</h3><pre id="command-stderr" class="v2-pre"></pre><button class="btn" id="command-read">读取后续已保存输出</button></div>');
  modal("原命令会话",content);let stdout=0,stderr=0;
  const read=async()=>{const button=$("#command-read",content);button.disabled=true;try{
    const result=await rpc("runs.command",{project_id:app.state.project.id,run_id:runId,command_session_id:identity,stdout_offset:stdout,stderr_offset:stderr});
    $("#command-status",content).textContent=result.state==="unknown"?"执行归属已丢失 · 停止未确认":result.process_stopped?`进程已停止 · 退出码 ${result.exit_code??"未知"}${result.termination?" · "+result.termination:""}`:"命令仍在运行";
    $("#command-environment",content).innerHTML=`<dt>后端</dt><dd>${escape(result.environment.backend)}</dd><dt>工作目录</dt><dd>${escape(result.cwd)}</dd><dt>环境修订</dt><dd>${escape(result.environment.revision)}</dd>`;
    $("#command-capture",content).textContent=result.output_refs.some(value=>value.truncated===true)?"部分输出未被保留；下载只包含已保存字节。":result.state==="unknown"?"已保存的原日志仍可读取；原始输出完整性未知。":"下载不会重新执行命令；运行中的下载是当前保存内容的快照。";
    $("#command-downloads",content).innerHTML=result.output_refs.filter(value=>value.available).map(value=>`<a class="btn btn-small" href="${escape(value.download_url)}" download>下载 ${escape(value.stream)} · ${value.saved_bytes} 字节</a>`).join("");
    $("#command-stdout",content).textContent+=result.stdout;$("#command-stderr",content).textContent+=result.stderr;
    stdout=result.stdout_next_offset;stderr=result.stderr_next_offset;
  }catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  $("#command-read",content).onclick=read;await read();
}
export async function showCandidates(app,runId){
  const project_id=app.state.project.id;
  const content=element('<div><p class="v2-muted">候选记录文件来源与版本，不代表已发布或已验收。版本复核只在点击时读取当前文件。</p><label class="field">候选来源<select id="candidate-source"><option value="patches">补丁回执</option><option value="outputs">本次运行输出目录</option></select></label><div id="candidate-directory" class="inline-actions"></div><div id="candidate-list"></div><p id="candidate-notice" class="v2-muted"></p><div class="inline-actions"><button class="btn" id="candidate-refresh">重新读取</button><button class="btn" id="candidate-more" hidden>下一页</button></div></div>');
  modal("文件候选与来源版本",content);let cursor=0,version=null,directory="",loading=false;
  const read=async(reset=false)=>{
    if(loading)return;loading=true;
    for(const selector of ["#candidate-source","#candidate-refresh","#candidate-more"])$(selector,content).disabled=true;
    for(const button of content.querySelectorAll("[data-directory]"))button.disabled=true;
    $("#candidate-notice",content).textContent="正在读取候选…";
    if(reset){cursor=0;version=null;}
    try{
      const source=$("#candidate-source",content).value;
      const page=await rpc("artifacts.candidates",{project_id,run_id:runId,source,cursor,...(source==="outputs"?{directory,version}:{})});
      $("#candidate-list",content).innerHTML=page.items.map(item=>`<section class="v2-panel"><strong>${escape(item.name)}</strong><pre class="v2-pre">${escape(item.path)}</pre><p>${item.published===true?"当前内容已有发布副本":item.published===false?"此版本尚未发布":"发布状态待内容校验"} · ${item.bytes} 字节 · ${item.sha256?escape(item.sha256):"尚未校验内容"}</p><p data-candidate-state="${escape(item.reference)}" class="v2-muted">保留原观察；当前来源尚未复核。</p><button class="btn btn-small" data-candidate-check="${escape(item.reference)}">复核当前文件版本</button></section>`).join("")||'<p class="quiet-state">此页没有文件候选。工作目录中的其他文件可通过实例命令查看。</p>';
      $("#candidate-directory",content).innerHTML=source==="outputs"?`<span>输出目录 / ${escape(directory)}</span>${directory?'<button class="btn btn-small" data-directory="">返回输出根目录</button>':""}${(page.directories||[]).map(path=>`<button class="btn btn-small" data-directory="${escape(path)}">${escape(path)}/</button>`).join("")}`:"";
      $("#candidate-notice",content).textContent=page.truncated?"当前目录仅观察前 1000 项。请选择更具体的目录，或用命令核验其余文件。":"文件需要明确发布后才会出现在工作产物中。";
      cursor=page.cursor;version=page.version||null;$("#candidate-more",content).hidden=cursor==null;
      for(const button of content.querySelectorAll("[data-directory]"))button.onclick=()=>{directory=button.dataset.directory;read(true);};
      for(const button of content.querySelectorAll("[data-candidate-check]"))button.onclick=async()=>{
        button.disabled=true;
        try{
          const result=await rpc("artifacts.candidate",{project_id,reference:button.dataset.candidateCheck,check_source:true});
          const labels={unchanged:"内容与原候选版本一致。",changed:"来源已经变化，请复核引用旧版本的结论。",missing:"来源文件已不存在；原候选记录仍保留。",first_verification:"已完成首次内容校验，原观察只记录了文件元数据。"};
          const target=button.parentElement.querySelector("[data-candidate-state]");target.textContent=labels[result.source_state]||result.source_state;
          if(result.current?.sha256)target.textContent+=" 当前 SHA-256："+result.current.sha256;
          if(result.current?.published!=null)target.textContent+=result.current.published?" · 当前内容已有发布副本。":" · 当前内容尚未发布。";
        }catch(error){notify(error.message,true);}finally{button.disabled=false;}
      }
    }catch(error){$("#candidate-notice",content).textContent=error.message;notify(error.message,true);}finally{
      loading=false;for(const selector of ["#candidate-source","#candidate-refresh","#candidate-more"])$(selector,content).disabled=false;
      for(const button of content.querySelectorAll("[data-directory]"))button.disabled=false;
    }
  };
  $("#candidate-source",content).onchange=()=>{directory="";read(true);};
  $("#candidate-refresh",content).onclick=()=>read(true);$("#candidate-more",content).onclick=()=>read();await read();
}
function resumeRun(run,refresh){
  const form=element(`<form><p>沿原检查点和预算继续，已完成的动作保留原回执。</p><label class="field">补充输入<textarea name="input" rows="4" maxlength="1048576"></textarea></label><label class="field">本次图执行步数上限（可选）<input name="max_steps" type="number" min="1" max="100000" step="1" placeholder="沿用最近有效值"></label><p class="form-hint">步数按图的 super-step 计算；累计调用与 token 预算继续生效。</p><button class="btn primary">继续执行</button></form>`);
  const close=modal("继续原工作",form),key=crypto.randomUUID();
  form.onsubmit=async event=>{
    event.preventDefault();const button=form.querySelector("button");button.disabled=true;
    try{
      const params={run_id:run.id,idempotency_key:key},input=form.elements.input.value,limit=form.elements.max_steps.value;
      if(input)params.input=input;if(limit)params.execution_limits={max_steps:Number(limit)};
      await rpc("runs.resume",params);close();await refresh();
    }catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
}
function confirmAction(app,action,refresh){
  const form=element(`<form><p class="v2-muted">先核对外部系统或文件中的真实效果，再记录原动作结果。此操作保存核对证据；后续继续工作需单独发起。</p><p><code>${escape(action.id)}</code></p><div class="field"><label for="confirmation-outcome">核对结论</label><select id="confirmation-outcome"><option value="completed">原动作已经完成，填入实际结果</option><option value="not_applied">已确认原动作没有产生效果</option></select></div><div class="field"><label for="confirmation-evidence">核对依据</label><textarea id="confirmation-evidence" rows="4" required maxlength="8192" placeholder="说明核对的外部回执、原文件或其他可验证事实。"></textarea></div><div class="field" id="confirmation-result-field"><label for="confirmation-result">原动作实际结果（JSON 对象）</label><textarea id="confirmation-result" rows="7" required placeholder="按原工具的结果契约填写。"></textarea></div><p><button class="btn primary">保存核对证据</button></p></form>`);
  const close=modal("核对原动作",form),key=crypto.randomUUID(),outcome=$("#confirmation-outcome",form),result=$("#confirmation-result",form);
  outcome.onchange=()=>{const completed=outcome.value==="completed";$("#confirmation-result-field",form).hidden=!completed;result.required=completed;};
  form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector("button");button.disabled=true;try{await rpc("runs.confirm_action",{action_id:action.id,project_id:app.state.project.id,idempotency_key:key,evidence:$("#confirmation-evidence",form).value,outcome:outcome.value,result:outcome.value==="completed"?JSON.parse(result.value):null});close();notify("核对结果已保存在原回执中。请检查后继续原工作。");await refresh();}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
}
export async function showAction(identity,selectedAttempt=null){let offset=0,attempt=selectedAttempt;const content=element('<div><p class="v2-muted" id="action-version"></p><p class="v2-muted" id="action-integrity"></p><pre class="v2-pre" id="action-text"></pre><button class="btn" id="action-next">读取下一页</button></div>');modal("原动作回执",content);const read=async()=>{try{const params={action_id:identity,offset,limit:8192};if(attempt)params.attempt_id=attempt;const page=await rpc("runs.action",params);attempt=page.attempt_id||attempt;$("#action-version",content).textContent=attempt?"模型尝试："+attempt+" · 继续读取保持该版本":"原动作结果";$("#action-integrity",content).textContent=integrityLabel(page.content_view?.integrity)+(page.offset_unit?" · 续读单位："+page.offset_unit:"");$("#action-text",content).textContent=page.text||JSON.stringify(page.error,null,2)||"原结果尚不可用";$("#action-next",content).hidden=page.next_offset==null;offset=page.next_offset;}catch(error){notify(error.message,true);}};$("#action-next",content).onclick=read;await read();}
async function showAttempts(app,identity){let cursor=0;const content=element('<div><p class="v2-muted">旧部分响应和错误保留在原尝试中。读取不会重新调用模型。</p><div id="attempt-list"></div><button class="btn" id="attempt-more">更早尝试</button></div>');modal("模型尝试历史",content);const read=async()=>{try{const page=await rpc("runs.attempts",{action_id:identity,project_id:app.state.project.id,cursor,limit:25});$("#attempt-list",content).innerHTML=page.items.map(item=>`<section class="v2-panel"><p>${date(item.created_at)} · ${escape(item.attempt_id)}</p><p class="v2-muted">${escape(item.error?.safe_message||"原调用失败")}</p><button class="btn" data-attempt="${escape(item.attempt_id)}">读取原响应与错误</button></section>`).join("")||'<p class="v2-muted">没有更早的失败尝试。</p>';for(const button of content.querySelectorAll("[data-attempt]"))button.onclick=()=>showAction(identity,button.dataset.attempt);cursor=page.cursor;$("#attempt-more",content).hidden=cursor==null;}catch(error){notify(error.message,true);}};$("#attempt-more",content).onclick=read;await read();}
function newRun(app){const s=app.state, key=crypto.randomUUID(),name="work-"+key.slice(0,8);const form=element(`<form><div class="field"><label for="run-agent">Agent 能力</label><select id="run-agent">${s.agents.filter(row=>row.definition.enabled).map(row=>`<option value="${escape(row.definition.id)}">${escape(row.definition.name)}</option>`).join("")}</select></div><div class="field"><label for="run-instance">继续已有实例（可选）</label><select id="run-instance"><option value="">创建新的独立实例</option>${s.instances.filter(value=>value.role==="main").map(value=>`<option value="${escape(value.id)}">${escape(value.name)}</option>`).join("")}</select></div><div class="field"><label for="run-goal">工作目标与交付要求</label><textarea id="run-goal" rows="7" required maxlength="1048576" placeholder="说明目标、输入来源和可验证的交付。"></textarea></div><p><button class="btn primary">提交明确工作</button></p></form>`);const close=modal("新工作",form);form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector("button");button.disabled=true;try{let instance=$("#run-instance",form).value;if(!instance)instance=(await rpc("instances.create",{session_id:s.session.id,agent_id:$("#run-agent",form).value,name})).id;const run=await rpc("runs.submit",{spec:{instance_id:instance,input:$("#run-goal",form).value,idempotency_key:key}});close();await app.reloadInstances();await renderRuns(app,$("#page"));await showRun(app,run.id);}catch(error){notify(error.message,true);}finally{button.disabled=false;}};}
