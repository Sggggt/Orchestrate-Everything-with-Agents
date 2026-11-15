// Attempt identities and sequence numbers reconcile transient text with receipts.
export const liveStates=new Set(["accepted","running"]);
const endings=new Set(["run.completed","run.failed","run.cancelled","run.needs_review","run.waiting"]);
const maxReconnects=5;

export function observeRun({runId,projectId,listeners,onChange,onSync,readResponse,source=url=>new EventSource(url)}){
  const state={text:"",previous:null,actionId:null,attemptId:null,attemptPosition:0,sequence:0,blockId:"content",note:"正在连接执行进度…",connected:false,run:null,saved:false};
  let stopped=false,position=0,timer=null,generation=0,hydration=null,wire=null,retryTimer=null,retries=0,connectionNote="",modelReconnect=null;
  const paint=()=>{clearTimeout(timer);timer=null;if(!stopped)onChange({...state,note:connectionNote||state.note});};
  const schedule=()=>{if(timer===null)timer=setTimeout(paint,80);};
  const sync=()=>{if(!stopped)onSync();};
  function adopt(frame){
    if(!frame.action_id||!frame.attempt_id)return false;
    if(frame.attempt_id===state.attemptId&&frame.action_id===state.actionId)return true;
    if(frame.attempt_position<=state.attemptPosition)return false;
    const previous=frame.action_id===state.actionId?(state.text?{actionId:state.actionId,attemptId:state.attemptId,text:state.text}:state.previous):null;
    Object.assign(state,{actionId:frame.action_id,attemptId:frame.attempt_id,attemptPosition:frame.attempt_position,
      sequence:0,blockId:frame.block_id||"content",text:"",previous,saved:false});
    return true;
  }
  function delta(frame){
    if(!adopt(frame)||state.saved||frame.sequence<=state.sequence)return;
    if(frame.sequence!==state.sequence+1){hydrate({action_id:state.actionId,attempt_id:state.attemptId});return;}
    const first=!state.text;
    state.text+=frame.text;state.sequence=frame.sequence;state.note="正在输出…";modelReconnect=null;
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
        state.note=modelReconnect|| (snapshot.unsaved_gap?"临时正文尚无已保存回执 · 等待原请求结算":state.saved?"正在载入已保存的结果…":"正在输出…");
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
      if(adopt({...data,attempt_position:value.position})&&!state.text&&!state.saved){
        modelReconnect=data.reconnect?"正在重连":null;
        state.note=modelReconnect||"正在准备请求…";schedule();
      }
    }
    const current=data.attempt_id===state.attemptId;
    if(value.kind==="model.reconnecting"&&(!state.attemptId||current)){
      modelReconnect="正在重连";state.note=modelReconnect;paint();
    }
    if(current&&value.kind==="model.send_started"&&!state.text){state.note=modelReconnect||"正在连接模型…";schedule();}
    if(current&&value.kind==="model.response_received"&&!state.text){modelReconnect=null;state.note="已收到响应，等待内容…";schedule();}
    if(current&&(value.kind==="model.completed"||value.kind==="model.failed")){
      modelReconnect=null;
      state.note=value.kind==="model.failed"?"本轮输出中断，正在读取已保存的结果":"正在整理执行结果…";paint();sync();
      hydrate({action_id:state.actionId,attempt_id:state.attemptId});
    }
    if(value.kind==="action.accepted"){state.note="正在调用 "+(data.tool||"工具");paint();}
    if(value.kind.startsWith("run.")||value.kind.startsWith("action.")||value.kind==="command.settled")sync();
  }
  const listener=value=>receive({kind:"event",event:value});
  listeners?.add(listener);
  function connect(){
    const params=new URLSearchParams({run_id:runId,project_id:projectId});
    if(position)params.set("after",position);
    const stream=wire=source("/api/events?"+params);
    stream.onopen=()=>{
      if(stopped||wire!==stream)return;
      generation++;hydration=null;retries=0;connectionNote="";state.connected=true;state.note=modelReconnect||"正在核对执行进度…";schedule();hydrate();
    };
    stream.onerror=()=>{if(!stopped&&wire===stream)disconnected();};
    stream.onmessage=event=>{if(wire===stream)receive(JSON.parse(event.data));};
  }
  function disconnected(){
    wire?.close();wire=null;generation++;hydration=null;state.connected=false;
    if(retries===maxReconnects){connectionNote="重连未成功 · 可重新打开对话查看回执";paint();return;}
    retries++;connectionNote="正在重连";paint();
    retryTimer=setTimeout(()=>{retryTimer=null;connect();},250*2**(retries-1));
  }
  function receive(frame){
    if(stopped)return;
    if(hydration&&(frame.kind==="model.delta"||frame.kind==="event")){
      if(hydration.frames.length<4096)hydration.frames.push(frame);else hydration.overflow=true;
      return;
    }
    if(frame.kind==="event"){fact(frame.event);return;}
    if(frame.kind==="model.delta")delta(frame);
    else if(frame.kind==="tool.observed"){state.note=frame.command_session_id?(frame.state==="running"?"命令仍在运行 · 已保存当前输出":frame.state==="unknown"?"命令状态待核对":frame.exit_code!=null?"命令已退出 · 退出码 "+frame.exit_code:frame.stop_confirmed?"命令已停止":"命令回执已保存"):(frame.isError?"工具返回错误 · ":"已运行 ")+frame.tool;paint();sync();}
    else if(frame.kind==="program.progress"){state.note=frame.text;schedule();}
    else if(frame.kind==="run.snapshot"){state.run=frame.run;schedule();sync();hydrate();}
    else if(frame.kind==="resync"){state.note="正在补读原响应…";schedule();hydrate();}
    else if(endings.has(frame.kind)){
      state.run=frame.run||{id:runId,state:frame.kind.slice(4)};
      connectionNote="";
      const count=state.run.error?.details?.reconnect_attempts;
      state.note=frame.kind==="run.completed"?"正在载入已保存的结果…":count?"重连未成功 · 请查看任务详情":state.text||state.previous?"本轮输出已结束 · 请在任务详情中核对回执":"";
      paint();sync();close();
    }else if(frame.kind==="host.closed"){
      disconnected();
    }
  }
  function close(){if(stopped)return;stopped=true;generation++;hydration=null;wire?.close();wire=null;clearTimeout(timer);clearTimeout(retryTimer);listeners?.delete(listener);}
  connect();
  return {close};
}
