import { $,escape,icon,iconButton,element,notify } from "./ui.mjs";

const tokens=/(#[^\r\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:from|import|as|def|class|return|await|async|with|if|else|for|in|True|False|None|true|false|null)\b|\b\d+(?:\.\d+)?\b)/g;
export function highlightLine(line){
  let html="",offset=0;
  for(const match of line.matchAll(tokens)){
    html+=escape(line.slice(offset,match.index));
    const value=match[0],kind=value.startsWith("#")?"comment":/^["']/.test(value)?"string":/^\d/.test(value)?"number":"keyword";
    html+=`<span class="syntax-${kind}">${escape(value)}</span>`;offset=match.index+value.length;
  }
  return html+escape(line.slice(offset));
}
export function codeViewer(source,{filename="source.py",language="python",download=false}={}){
  const lines=source.split(/\r?\n/);
  const root=element(`<section class="source-viewer"><header class="source-toolbar"><span class="source-filename">${icon("code")}<strong title="${escape(filename)}">${escape(filename)}</strong></span><span class="source-language">${escape(language)}</span><span class="source-readonly" title="只读源码" aria-label="只读源码">${icon("lock")}</span><div class="source-actions">${iconButton("copy","复制源码",{"data-copy":true})}${download?iconButton("download","下载源码",{"data-download":true}):""}</div></header><pre class="source-code" tabindex="0" role="region" aria-label="${escape(language)} 源码">${lines.map((line,index)=>`<span class="source-line"><span class="line-number" aria-hidden="true">${index+1}</span><code class="code-text">${highlightLine(line)}</code></span>`).join("")}</pre><footer class="source-status"><span>${lines.length} 行</span><span>UTF-8</span></footer></section>`);
  $("[data-copy]",root).onclick=async()=>{try{await navigator.clipboard.writeText(source);notify("源码已复制");}catch{notify("无法复制，请选择源码后复制",true);}};
  if(download)$("[data-download]",root).onclick=()=>{const url=URL.createObjectURL(new Blob([source],{type:language==="python"?"text/x-python;charset=utf-8":"text/plain;charset=utf-8"})),link=document.createElement("a");link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),0);};
  return root;
}
