// Original artifact grouping, cards, upload picker and read-only source modal.
import { $,escape,icon,rpc,element,notify,date } from "./ui.mjs";
import { avatar } from "./workbench-shell.mjs";
import { artifactDownload,artifactSource,groupArtifacts,readArtifactPreview } from "./artifacts.mjs";

const pages=new WeakMap();
export const formatBytes=bytes=>bytes<1024?`${bytes} B`:bytes<1024*1024?`${(bytes/1024).toFixed(1)} KB`:`${(bytes/1024/1024).toFixed(1)} MB`;
function card(item){return `<article class="artifact-card"><button class="artifact-card-open" data-artifact="${escape(item.id)}" data-focus="artifact-${escape(item.id)}" aria-label="查看原文：${escape(item.name)}"><span class="file-symbol">${icon("file")}</span><h3 title="${escape(item.name)}">${escape(item.name)}</h3><p>${formatBytes(item.bytes)}<span>·</span>${date(item.created_at)}</p><span class="artifact-open-hint">查看原文 ${icon("arrow")}</span></button><a class="icon-btn artifact-download" href="${artifactDownload(item.id,item.project_id)}" aria-label="下载 ${escape(item.name)}" title="下载产物">${icon("download")}</a></article>`;}

export function renderArtifactsPage(app,root){
  const s=app.state,scope=s.project.id+":"+s.session?.id,signature=(s.artifacts[0]?.id||"")+":"+(s.summary.artifacts||0);
  let page=pages.get(app);
  if(!page||page.scope!==scope){page={scope,items:s.artifacts,cursor:s.artifactCursor,query:"",signature,epoch:0};pages.set(app,page);}
  if(page.signature!==signature&&!page.query){page.items=s.artifacts;page.cursor=s.artifactCursor;page.signature=signature;}
  page.epoch++;
  const active=()=>root.isConnected&&app.state.page==="artifacts"&&scope===app.state.project?.id+":"+app.state.session?.id;
  const results=()=>{
    const groups=groupArtifacts(page.items,s.agents.map(row=>row.definition));
    $("#artifact-results",root).innerHTML=groups.length?`<div class="artifacts-by-agent">${groups.map(({source,items})=>`<section class="artifact-source-group" data-source-key="${escape(source.key)}"><div class="section-heading"><h2>${source.kind==="agent"?avatar(s,source.agent_id):icon(source.kind==="upload"?"clip":"file")}${escape(source.label)}</h2><span class="artifact-group-count">${items.length} 项产物</span></div><div class="artifact-grid">${items.map(card).join("")}</div></section>`).join("")}</div>`:`<div class="empty"><h3>${page.query?"没有匹配的产物":"还没有工作产物"}</h3><div>${page.query?"试试文件名或来源 Agent 的名称。":"Agent 交付的成果和你上传的文件会保存在当前会话。"}</div><button class="btn" data-upload>添加产物</button></div>`;
    $("#artifact-more",root).hidden=page.cursor==null;
    $(".artifact-total",root).textContent=`已载入 ${page.items.length} 个文件 · ${formatBytes(page.items.reduce((sum,item)=>sum+item.bytes,0))}`;
    for(const button of root.querySelectorAll("[data-artifact]"))button.onclick=()=>previewArtifact(app,button.dataset.artifact);
    for(const button of root.querySelectorAll("[data-upload]"))button.onclick=()=>uploadArtifact(app);
  };
  root.innerHTML=`<section class="panel-page"><div class="page-heading"><div><h1>工作产物</h1></div><button class="icon-btn" data-upload title="添加产物" aria-label="添加产物">${icon("plus")}</button></div><div class="filter-toolbar"><span class="artifact-total"></span><label class="inline-search">${icon("search")}<input id="artifact-search" data-focus="artifact-search" value="${escape(page.query)}" maxlength="200" placeholder="搜索文件或来源 Agent…" aria-label="搜索文件或来源 Agent"></label></div><div id="artifact-results"></div><button class="btn" id="artifact-more">继续读取产物</button></section>`;
  const load=async more=>{
    const epoch=++page.epoch,query=page.query;
    if(!s.session)return;
    const next=await rpc("artifacts.list",{project_id:s.project.id,session_id:s.session.id,query,limit:25,cursor:more?page.cursor:0});
    if(!active()||epoch!==page.epoch)return;
    page.items=more?[...page.items,...next.items]:next.items;page.cursor=next.cursor;page.signature=signature;results();
  };
  let timer;
  $("#artifact-search",root).oninput=event=>{page.query=event.target.value;page.epoch++;clearTimeout(timer);timer=setTimeout(()=>{if(active())load(false).catch(error=>notify(error.message,true));},200);};
  $("#artifact-more",root).onclick=async event=>{const button=event.target;button.disabled=true;try{await load(true);}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
  results();
  if(page.query&&page.signature!==signature)load(false).catch(error=>notify(error.message,true));
}

export async function uploadArtifact(app,onUploaded){
  const project_id=app.state.project?.id,session_id=app.state.session?.id;
  if(!session_id){notify("先创建一个会话");return;}
  const input=document.createElement("input");input.type="file";
  input.onchange=async()=>{try{
    const file=input.files[0];if(!file)return;
    if(file.size>50*1024*1024)throw new Error("文件不能超过 50 MB。");
    const hex=buffer=>[...new Uint8Array(buffer)].map(value=>value.toString(16).padStart(2,"0")).join("");
    const sha256=hex(await crypto.subtle.digest("SHA-256",await file.arrayBuffer()));
    const slot="oea-pending-upload:"+hex(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(JSON.stringify([project_id,session_id,file.name,sha256]))));
    const key=localStorage.getItem(slot)||crypto.randomUUID();localStorage.setItem(slot,key);
    const receipt=await rpc("artifacts.begin_upload",{project_id,session_id,name:file.name,bytes:file.size,sha256,idempotency_key:key});
    if(receipt.state!=="completed"){
      const response=await fetch(`/api/artifact-uploads/${encodeURIComponent(receipt.id)}/content?project_id=${encodeURIComponent(project_id)}`,{method:"PUT",headers:{"Content-Type":"application/octet-stream","X-OEA-Protocol-Version":"2.0","X-OEA-Consumer-ID":localStorage.getItem("oea-consumer")},body:file});
      const result=await response.json();if(result.error)throw new Error(result.error.safe_message);if(!response.ok||result.result?.state!=="completed")throw new Error("请读取原上传回执，核对文件是否已完整接收。");
    }
    localStorage.removeItem(slot);notify("产物已收存");onUploaded?.({id:receipt.id,name:receipt.name});
    if(app.state.project?.id===project_id&&app.state.session?.id===session_id){await app.reloadInstances();if(app.state.page==="artifacts")renderArtifactsPage(app,$("#page"));else if(app.state.page==="overview")await app.render();}
  }catch(error){notify(error.message,true);}};
  input.click();
}

export async function previewArtifact(app,identity){
  const project_id=app.state.project.id,origin=document.activeElement;
  try{
    const item=await rpc("artifacts.get",{artifact_id:identity});if(app.state.project?.id!==project_id)return;
    let known=app.state.artifacts.find(value=>value.id===identity)||item;
    if(item.origin==="run"&&!known.agent_id){const instance=await rpc("instances.get",{instance_id:item.instance_id});known={...known,agent_id:instance.agent_id};}
    if(app.state.project?.id!==project_id)return;
    const source=artifactSource(known,app.state.agents.map(row=>row.definition));
    const backdrop=element(`<div class="modal-backdrop"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="artifact-dialog-title"><header><h2 id="artifact-dialog-title">${escape(item.name)}</h2><button class="icon-btn" data-close aria-label="关闭对话框">${icon("close")}</button></header><div class="modal-content"><div class="artifact-preview-meta"><span>只读原文</span><span>${escape(source.label)}</span><span>${formatBytes(item.bytes)}</span></div><div data-preview><p class="loading-inline" role="status">正在读取产物原文…</p></div></div><footer><button class="btn" data-copy hidden>复制原文</button><a class="btn" href="${artifactDownload(item.id,project_id)}">${icon("download")}下载文件</a><button class="btn primary" data-close>关闭</button></footer></section></div>`);
    let closed=false;const close=()=>{closed=true;backdrop.remove();$("#app").inert=false;document.removeEventListener("keydown",keys,true);if(origin?.isConnected)origin.focus();document.dispatchEvent(new Event("oea-modal-close"));};
    const keys=event=>{if(event.key==="Escape"){event.preventDefault();close();}if(event.key==="Tab"){const items=[...backdrop.querySelectorAll("button,a[href],pre[tabindex]")].filter(value=>!value.hidden&&!value.disabled);if(event.shiftKey&&document.activeElement===items[0]){event.preventDefault();items.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===items.at(-1)){event.preventDefault();items[0]?.focus();}}};
    for(const button of backdrop.querySelectorAll("[data-close]"))button.onclick=close;
    $("#app").inert=true;$("#modal-root").append(backdrop);document.addEventListener("keydown",keys,true);$("[data-close]",backdrop).focus();
    try{const preview=await readArtifactPreview(item);if(closed)return;
      const body=$("[data-preview]",backdrop);
      if(preview.kind==="text"){
        body.innerHTML=`${item.content_view?.source_complete===false?'<p class="artifact-preview-notice">原始输出已达到捕获上限，文件仅包含已保存部分。</p>':""}${preview.truncated?'<p class="artifact-preview-notice">文件较大，仅显示前 1 MB；下载可获取已保存文件。</p>':""}${preview.text?'<pre class="artifact-source" tabindex="0" role="region" aria-label="产物原文"><code></code></pre>':'<p class="quiet-state">这是一个空文本文件。</p>'}`;
        if(preview.text)$("code",body).textContent=preview.text;
        const copy=$("[data-copy]",backdrop);copy.hidden=false;copy.textContent=preview.truncated?"复制已显示文本":"复制原文";copy.onclick=()=>navigator.clipboard.writeText(preview.text).then(()=>notify("已复制产物原文"),error=>notify(error.message,true));
      }else body.innerHTML=`<p class="quiet-state">${escape(preview.message)}</p>`;
    }catch(error){if(!closed)$("[data-preview]",backdrop).innerHTML=`<p class="artifact-preview-error" role="alert">${escape(error.message)}</p>`;}
  }catch(error){notify(error.message,true);}
}
