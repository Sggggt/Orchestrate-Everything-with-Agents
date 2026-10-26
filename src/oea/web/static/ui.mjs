export const $=(selector,root=document)=>root.querySelector(selector);
export const escape=(value="")=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const paths = {
  plug: "M8 2v5 M16 2v5 M6 7h12v4a6 6 0 0 1-6 6v5 M12 17a6 6 0 0 1-6-6",
  book: "M4 3h7a3 3 0 0 1 3 3v15a4 4 0 0 0-4-2H4z M14 6a3 3 0 0 1 3-3h4v16h-3a4 4 0 0 0-4 2",
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  agents:
    "M8 8a4 4 0 1 0 8 0a4 4 0 0 0-8 0 M4 21v-2a8 8 0 0 1 16 0v2 M3 8H1 M23 8h-2",
  settings:
    "M10 2h4l.5 3 2 1.2 2.8-1 2 3.4-2.3 2v2.8l2.3 2-2 3.4-2.8-1-2 1.2-.5 3h-4l-.5-3-2-1.2-2.8 1-2-3.4 2.3-2v-2.8l-2.3-2 2-3.4 2.8 1 2-1.2Z M12 8a4 4 0 1 0 0 8a4 4 0 0 0 0-8",
  chat: "M21 11a8 8 0 0 1-8 8H6l-4 3V11a8 8 0 0 1 8-8h3a8 8 0 0 1 8 8 M7 9h9 M7 13h6",
  arrow: "M5 12h14 M14 7l5 5-5 5",
  plus: "M12 5v14 M5 12h14",
  trash: "M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7",
  flow: "M4 3h6v6H4z M14 15h6v6h-6z M7 9v9h7 M10 6h7v9",
  mail: "M3 5h18v14H3z M3 5l9 8 9-8",
  send: "M22 2L9 15 M22 2l-7 20-6-7-7-6z",
  terminal: "M5 6l6 6-6 6 M13 18h7",
  shield: "M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M8 12l3 3 5-6",
  branch:
    "M6 3v12a4 4 0 0 0 4 4h8 M6 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4 M18 5v6a4 4 0 0 1-4 4H6 M18 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4",
  clip: "M8 14l7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l10-10",
  file: "M5 2h9l5 5v15H5z M14 2v6h5 M8 13h8 M8 17h6",
  close: "M6 6l12 12 M6 18L18 6",
  refresh:
    "M20 7v5h-5 M4 17v-5h5 M5 7a8 8 0 0 1 14-2l1 7 M4 12l1 7a8 8 0 0 0 14-2",
  clock: "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18 M12 7v6l4 2",
  check: "M5 12l4 4L19 6",
  external: "M14 3h7v7 M21 3L10 14 M10 3H3v18h18v-7",
  search: "M11 3a8 8 0 1 0 0 16a8 8 0 0 0 0-16 M17 17l4 4",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  chevron: "M9 5l7 7-7 7",
  spark: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z",
  activity: "M2 12h4l3-8 6 16 3-8h4",
  list: "M9 6h12 M9 12h12 M9 18h12 M3 6h1 M3 12h1 M3 18h1",
  board: "M3 4h5v16H3z M10 4h5v11h-5z M17 4h5v14h-5z",
  panel: "M3 4h18v16H3z M15 4v16",
  upload: "M12 16V3 m-5 5 5-5 5 5 M4 16v5h16v-5",
  reply: "M9 5l-6 6 6 6 M3 11h11a7 7 0 0 1 7 7",
  download: "M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5",
  folder: "M3 7V4h6l3 3h9v13H3z",
};
export const icon=name=>`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.agents}"/></svg>`;
export const stateLabel={accepted:"已接受",running:"执行中",waiting:"等待输入或依赖",completed:"交付完成",failed:"执行失败",cancelled:"已取消",needs_review:"结果待核对",skipped:"前置工作未完成"};
export const badge=(state,node=false)=>`<span class="v2-state ${escape(state)}"><i class="dot ${escape(state)}"></i>${escape(node&&state==="completed"?"已完成":stateLabel[state]||state)}</span>`;
export const date=value=>new Date(value).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
export function notify(message,error=false) {const root=$("#toast"); root.textContent=message; root.className=error?"visible error":"visible"; clearTimeout(notify.timer); notify.timer=setTimeout(()=>root.className="",4500);}
export async function rpc(operation,params={}) {
  let consumer=localStorage.getItem("oea-consumer"); if(!consumer){consumer="browser-"+crypto.randomUUID();localStorage.setItem("oea-consumer",consumer);}
  let response;
  try {response=await fetch("/api/rpc",{method:"POST",headers:{"Content-Type":"application/json","X-OEA-Protocol-Version":"2.0","X-OEA-Consumer-ID":consumer},body:JSON.stringify({protocol_version:"2.0",operation,params,...(operation==="messages.send"?{agent_name:"leader"}:{}),request_id:crypto.randomUUID()})});}
  catch {throw new Error("连接中断。请先查询原回执，再继续原工作。");}
  const body=await response.json(); if(body.error){const detail=body.error.details?.errors?.map(item=>item.path.join(".")+": "+item.message).join("\n");throw Object.assign(new Error(body.error.safe_message+(detail?"\n"+detail:"")),body.error);} if(!response.ok)throw new Error("本地服务暂不可用"); return body.result;
}
export function element(html) {const template=document.createElement("template");template.innerHTML=html.trim();return template.content.firstElementChild;}
export function modal(title,content,{size="default"}={}) {
  const previous=document.activeElement, backdrop=element(`<div class="v2-dialog-backdrop"><section class="v2-drawer dialog-${escape(size)}" role="dialog" aria-modal="true" aria-label="${escape(title)}"><header><h2>${escape(title)}</h2><button class="icon-btn" aria-label="关闭">${icon("close")}</button></header><div class="dialog-body"></div></section></div>`);
  $(".dialog-body",backdrop).append(content);$("#modal-root").append(backdrop);
  let closed=false;const close=()=>{if(closed)return;closed=true;close.onclose?.();backdrop.remove();if(previous?.isConnected)previous.focus({preventScroll:true});document.dispatchEvent(new Event("oea-modal-close"));};$("header button",backdrop).onclick=close;
  backdrop.addEventListener("keydown",event=>{if(event.key==="Escape"){event.stopPropagation();close();}if(event.key==="Tab"){const items=[...backdrop.querySelectorAll("button,input,textarea,select,a[href],summary")].filter(item=>!item.disabled&&item.getClientRects().length);if(event.shiftKey&&document.activeElement===items[0]){event.preventDefault();items.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===items.at(-1)){event.preventDefault();items[0]?.focus();}}});
  backdrop.querySelector("input,textarea,select,button")?.focus();return close;
}
export function empty(title,text,action="") {return `<div class="v2-empty"><img class="brand-mark" src="/assets/mark.svg" alt=""><h2>${escape(title)}</h2><p>${escape(text)}</p>${action}</div>`;}
