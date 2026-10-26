// Keep the original MCP trial dialog layout and observe the v2 action receipt.
import { $,escape,icon,rpc,element,notify } from "./ui.mjs";
import { content,schemaView,schemaDefaults,valueEditor,readEditor,editorInteraction,toolResult } from "./presentation.mjs";

export function showMCPTrial(app,project_id,server_id,tool,afterClose){
  const origin=document.activeElement;
  const backdrop=element(`<div class="modal-backdrop"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="mcp-trial-title"><header><h2 id="mcp-trial-title">${escape(tool.name)}</h2><button class="icon-btn" data-trial="close" aria-label="关闭对话框">${icon("close")}</button></header><div class="modal-content"></div><footer><button class="btn" data-trial="close">关闭</button><button class="btn primary" data-trial="run">${icon("terminal")}运行工具</button></footer></section></div>`);
  const state={arguments:schemaDefaults(tool.inputSchema),key:crypto.randomUUID(),receipt:null,error:"",sending:false,uncertain:false,closed:false};
  let refreshing=null,dirty=false;
  $("#app").inert=true;$("#modal-root").append(backdrop);
  const frozen=()=>state.sending||state.uncertain||state.receipt&&!['completed','failed'].includes(state.receipt.state);
  const draw=()=>{
    if(state.closed)return;
    const receipt=state.receipt, result=receipt?.result;
    const label=({intent:"已接受",dispatched:"执行中",completed:result?.isError?"工具返回错误":"已完成",failed:"未完成",needs_review:"待核对"})[receipt?.state]||"";
    $(".modal-content",backdrop).innerHTML=`${content(tool.description||"填写参数，试运行这个 MCP 工具。")}<div class="form-grid"><label class="field full">调用上下文<input id="mcp-consumer" data-focus="mcp-consumer" value="${escape(localStorage.getItem("oea-consumer")||"本机浏览器")}" readonly><small>回执归属当前调用方；重试沿用同一个调用键。</small></label><div class="field full"><span>调用参数</span>${valueEditor(state.arguments,"mcp-arguments",tool.inputSchema)}</div></div><details class="tool-schema"><summary>查看参数协议</summary>${schemaView(tool.inputSchema||{})}</details>${receipt?`<p class="form-hint" data-trial-receipt>${escape(label)} · <code>${escape(receipt.id)}</code>${receipt.cancel_requested_at?" · 已请求取消":""}</p>`:""}${state.error?`<div class="integration-error">${escape(state.error)}</div>`:""}${result?`<div class="section-heading"><h2>返回结果</h2><span class="badge ${result.isError?"FAILED":"SUCCEEDED"}">${result.isError?"工具返回错误":"完成"}</span></div><div class="tool-result rendered-result">${toolResult(result)}</div>`:""}`;
    for(const input of backdrop.querySelectorAll('[data-value-editor] input,[data-value-editor] textarea,[data-value-editor] select,[data-value-editor] button'))input.disabled=!!frozen();
    const run=$('[data-trial="run"]',backdrop);run.disabled=state.sending;run.innerHTML=icon("terminal")+(state.sending?"提交中…":receipt?"读取原回执":state.uncertain?"核对原调用":"运行工具");
  };
  const read=async()=>{
    if(!state.receipt||state.closed)return;
    dirty=true;if(refreshing)return refreshing;
    refreshing=(async()=>{do{dirty=false;const call_id=state.receipt?.id;if(!call_id)return;const receipt=await rpc("mcp.call_result",{call_id,project_id});if(state.closed)return;if(state.receipt?.id!==call_id){dirty=!!state.receipt;continue;}state.receipt=receipt;state.error=receipt.error?.safe_message||"";draw();}while(dirty&&!state.closed);})();
    try{await refreshing;}finally{refreshing=null;}
  };
  const observe=fact=>{if(fact.data?.call_id===state.receipt?.id)read().catch(error=>notify(error.message,true));};
  app.onToolEvent=observe;
  const close=()=>{
    if(state.closed)return;state.closed=true;
    if(app.onToolEvent===observe)app.onToolEvent=null;
    backdrop.remove();$("#app").inert=false;document.removeEventListener("keydown",keys,true);if(origin?.isConnected)origin.focus();afterClose?.().catch(error=>notify(error.message,true));
  };
  const keys=event=>{
    if(event.key==="Escape"){event.preventDefault();close();}
    if(event.key==="Tab"){
      const items=[...backdrop.querySelectorAll("button,input,textarea,select,a[href]")].filter(value=>!value.disabled&&value.getClientRects().length);
      if(event.shiftKey&&document.activeElement===items[0]){event.preventDefault();items.at(-1)?.focus();}
      else if(!event.shiftKey&&document.activeElement===items.at(-1)){event.preventDefault();items[0]?.focus();}
    }
  };
  document.addEventListener("keydown",keys,true);
  backdrop.oninput=event=>{
    if(frozen()||!event.target.closest('[data-value-editor="mcp-arguments"]'))return;
    try{state.arguments=readEditor($('[data-value-editor="mcp-arguments"]',backdrop));if(state.receipt){state.receipt=null;state.key=crypto.randomUUID();state.error="";$('[data-trial="run"]',backdrop).innerHTML=icon("terminal")+"运行工具";for(const value of backdrop.querySelectorAll("[data-trial-receipt],.tool-result,.section-heading,.integration-error"))value.remove();}event.target.setCustomValidity?.("");}
    catch(error){event.target.setCustomValidity?.(error.message);}
  };
  backdrop.onchange=event=>{if(editorInteraction(event))return;backdrop.oninput(event);};
  backdrop.onclick=async event=>{
    if(editorInteraction(event))return;
    const action=event.target.closest("[data-trial]")?.dataset.trial;
    if(action==="close"){close();return;}
    if(action!=="run"||state.sending)return;
    try{
      if(state.receipt){await read();return;}
      state.arguments=readEditor($('[data-value-editor="mcp-arguments"]',backdrop));
      state.sending=true;state.error="";draw();
      try{
        const receipt=await rpc("mcp.call",{server_id,tool_name:tool.name,arguments:state.arguments,idempotency_key:state.key,project_id,wait:false});
        if(state.closed)return;state.receipt=receipt;state.uncertain=false;state.sending=false;draw();await read();
      }catch(error){
        if(error.call_id){state.receipt={id:error.call_id};state.uncertain=false;state.sending=false;await read();}
        else{state.uncertain=!error.code;state.error=error.message;}
      }finally{state.sending=false;draw();}
    }catch(error){notify(error.message,true);}
  };
  draw();(backdrop.querySelector('[data-value-input]')||backdrop.querySelector("button"))?.focus();
}
