// Attempt identities and sequence numbers reconcile transient text with receipts.
export const liveStates=new Set(["accepted","running"]);
const endings=new Set(["run.completed","run.failed","run.cancelled","run.needs_review","run.waiting"]);

export function observeRun({runId,projectId,listeners,onChange,onSync,readResponse,source=url=>new EventSource(url)}){
  const state={text:"",actionId:null,attemptId:null,attemptPosition:0,sequence:0,blockId:"content",note:"正在连接执行进度…",connected:false,run:null,saved:false};
  let stopped=false,position=0,timer=null,generation=0,hydration=null;
  const paint=()=>{clearTimeout(timer);timer=null;if(!stopped)onChange({...state});};
  const schedule=()=>{if(timer===null)timer=setTimeout(paint,80);};
  const sync=()=>{if(!stopped)onSync();};
  function adopt(frame){
    if(!frame.action_id||!frame.attempt_id)return false;
    if(frame.attempt_id===state.attemptId&&frame.action_id===state.actionId)return true;
    if(frame.attempt_position<=state.attemptPosition)return false;
    Object.assign(state,{actionId:frame.action_id,attemptId:frame.attempt_id,attemptPosition:frame.attempt_position,
      sequence:0,blockId:frame.block_id||"content",text:"",saved:false});
    return true;
  }
  function delta(frame){
    if(!adopt(frame)||state.saved||frame.sequence<=state.sequence)return;
    if(frame.sequence!==state.sequence+1){hydrate({action_id:state.actionId,attempt_id:state.attemptId});return;}
    const first=!state.text;
    state.text+=frame.text;state.sequence=frame.sequence;state.note="正在输出…";
    if(first)paint();else schedule();
  }
  async function hydrate(params={}){
    if(!readResponse||stopped||hydration)return;
    const pending={generation,frames:[],overflow:false};hydration=pending;
    try{
      let page=await readResponse({run_id:runId,...params,limit:16384}),text=page.text||"";
      const snapshot=page;
      while(page.next_offset!=null){
        if(stopped||pending.generation!==generation)return;
        page=await readResponse({run_id:runId,action_id:snapshot.action_id,attempt_id:snapshot.attempt_id,
          version:snapshot.version,offset:page.next_offset,limit:16384});
        text+=page.text;
      }
      if(stopped||pending.generation!==generation)return;
      if(snapshot.available&&adopt(snapshot)){
        if(!snapshot.unsaved_gap){
          state.text=text;state.sequence=snapshot.sequence??state.sequence;state.saved=snapshot.status==="saved";
        }
        state.note=snapshot.unsaved_gap?"临时正文尚无已保存回执 · 等待原请求结算":state.saved?"正在载入已保存的结果…":"正在输出…";
        paint();
      }
    }catch(error){
      if(!stopped&&pending.generation===generation){state.note="补读未完成 · "+error.message;paint();}
    }finally{
      if(hydration===pending)hydration=null;
      if(!stopped&&pending.generation===generation){
        if(pending.overflow)hydrate();
        else for(const frame of pending.frames)receive(frame);
      }
    }
  }
  function fact(value){
    if(stopped||value.run_id!==runId||value.position<=position)return;
    position=value.position;
    const data=value.data||{};
    if(value.kind==="model.started"&&data.purpose!=="summary"){
      if(adopt({...data,attempt_position:value.position})){state.note="正在准备请求…";schedule();}
    }
    const current=data.attempt_id===state.attemptId;
    if(current&&value.kind==="model.send_started"){state.note="正在连接模型…";schedule();}
    if(current&&value.kind==="model.response_received"){state.note="已收到响应，等待内容…";schedule();}
    if(current&&(value.kind==="model.completed"||value.kind==="model.failed")){
      state.note=value.kind==="model.failed"?"本轮输出中断，正在读取已保存的结果":"正在整理执行结果…";paint();sync();
      hydrate({action_id:state.actionId,attempt_id:state.attemptId});
    }
    if(value.kind==="action.accepted"){state.note="正在调用 "+(data.tool||"工具");paint();}
    if(value.kind.startsWith("run.")||value.kind.startsWith("action."))sync();
  }
  const listener=value=>receive({kind:"event",event:value});
  listeners?.add(listener);
  const stream=source("/api/events?"+new URLSearchParams({run_id:runId,project_id:projectId}));
  stream.onopen=()=>{
    if(stopped)return;
    generation++;hydration=null;state.connected=true;state.note="正在核对执行进度…";schedule();hydrate();
  };
  stream.onerror=()=>{if(stopped)return;generation++;hydration=null;state.connected=false;state.note="连接中断，正在重连 · 已显示内容保留";paint();};
  function receive(frame){
    if(stopped)return;
    if(hydration&&(frame.kind==="model.delta"||frame.kind==="event")){
      if(hydration.frames.length<4096)hydration.frames.push(frame);else hydration.overflow=true;
      return;
    }
    if(frame.kind==="event"){fact(frame.event);return;}
    if(frame.kind==="model.delta")delta(frame);
    else if(frame.kind==="tool.observed"){state.note=(frame.isError?"工具返回错误 · ":"已运行 ")+frame.tool;paint();sync();}
    else if(frame.kind==="program.progress"){state.note=frame.text;schedule();}
    else if(frame.kind==="run.snapshot"){state.run=frame.run;schedule();sync();hydrate();}
    else if(frame.kind==="resync"){state.note="正在补读原响应…";schedule();hydrate();}
    else if(endings.has(frame.kind)){
      state.run=frame.run||{id:runId,state:frame.kind.slice(4)};
      state.note=frame.kind==="run.completed"?"正在载入已保存的结果…":state.text?"本轮输出已结束 · 请在任务详情中核对回执":"";
      paint();sync();close();
    }else if(frame.kind==="host.closed"){
      generation++;hydration=null;state.connected=false;state.note="宿主连接已关闭，等待恢复 · 已显示内容保留";paint();sync();
    }
  }
  stream.onmessage=event=>receive(JSON.parse(event.data));
  function close(){if(stopped)return;stopped=true;generation++;hydration=null;stream.close();clearTimeout(timer);listeners?.delete(listener);}
  return {close};
}
