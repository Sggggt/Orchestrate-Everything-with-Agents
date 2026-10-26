// One visible Run owns one stream. Durable facts reconcile the ephemeral text.
export const liveStates=new Set(["accepted","running"]);
const endings=new Set(["run.completed","run.failed","run.cancelled","run.needs_review","run.waiting"]);

export function observeRun({runId,projectId,listeners,onChange,onSync,source=url=>new EventSource(url)}){
  const state={text:"",actionId:null,note:"正在连接执行进度…",connected:false,run:null};
  let stopped=false,position=0,timer=null,hasConnected=false;
  const paint=()=>{timer=null;if(!stopped)onChange({...state});};
  const schedule=()=>{if(timer===null)timer=setTimeout(paint,80);};
  const sync=()=>{if(!stopped)onSync();};
  function fact(value){
    if(stopped||value.run_id!==runId||value.position<=position)return;
    position=value.position;
    if(value.kind==="model.started"){state.text="";state.actionId=value.data?.action_id;state.note="正在准备请求…";schedule();}
    if(value.kind==="model.send_started"){state.note="正在连接模型…";schedule();}
    if(value.kind==="model.response_received"){state.note="已收到响应，等待内容…";schedule();}
    if(value.kind==="model.completed"){state.actionId=value.data?.action_id;state.note="正在整理执行结果…";schedule();sync();}
    if(value.kind==="model.failed"){state.note="本轮输出中断，正在读取已保存的结果";schedule();sync();}
    if(value.kind==="action.accepted"){state.note="正在调用 "+(value.data?.tool||"工具");schedule();}
    if(value.kind.startsWith("run.")||value.kind.startsWith("action."))sync();
  }
  listeners?.add(fact);
  const stream=source("/api/events?"+new URLSearchParams({run_id:runId,project_id:projectId}));
  stream.onopen=()=>{state.connected=true;state.note="正在执行…";if(hasConnected){state.text="";state.note="连接已恢复，正在核对已保存的进度…";sync();}hasConnected=true;schedule();};
  stream.onerror=()=>{state.connected=false;state.note="连接中断，正在重连 · 已显示内容保留";schedule();};
  stream.onmessage=event=>{
    if(stopped)return;
    const frame=JSON.parse(event.data);
    if(frame.kind==="event"){fact(frame.event);return;}
    if(frame.kind==="model.delta"){state.text+=frame.text;state.note="正在输出…";schedule();}
    else if(frame.kind==="tool.observed"){state.note=(frame.isError?"工具返回错误 · ":"已运行 ")+frame.tool;schedule();sync();}
    else if(frame.kind==="program.progress"){state.note=frame.text;schedule();}
    else if(frame.kind==="run.snapshot"){state.run=frame.run;schedule();sync();}
    else if(frame.kind==="resync"){state.text="";state.note="正在同步已保存的进度…";schedule();sync();}
    else if(endings.has(frame.kind)){
      state.run=frame.run||{id:runId,state:frame.kind.slice(4)};
      state.note=frame.kind==="run.completed"?"正在载入已保存的结果…":state.text?"本轮输出已结束 · 请在任务详情中核对回执":"";paint();sync();close();
    }else if(frame.kind==="host.closed"){
      state.connected=false;state.note="宿主连接已关闭，等待恢复 · 已显示内容保留";paint();sync();
    }
  };
  function close(){if(stopped)return;stopped=true;stream.close();clearTimeout(timer);listeners?.delete(fact);}
  return {close};
}
