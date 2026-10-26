// The original MCP directory layout, using the shared v2 service.
import { $,escape,icon,rpc,notify,modal,element,date } from "./ui.mjs";
import { markdown } from "./markdown.mjs";
import { editorInteraction } from "./presentation.mjs";
import { createMCPDirectory } from "./mcp-directory.mjs";
import { showMCPTrial } from "./mcp-trial.mjs";

const controllers=new WeakMap();
export async function manageMCP(app,root){
  let projects=controllers.get(app);if(!projects){projects=new Map();controllers.set(app,projects);}
  const project=app.state.project;
  let entry=projects.get(project.id);
  if(!entry){
    const state={project,mcpServers:[],mcpQuery:"",mcpDirectory:"",mcpDraft:null,mcpDetail:null,mcpId:null,mcpTab:"connection",mcpCalls:[],mcpEpoch:0};
    let component;
    const mounted=new WeakSet();
    const current=()=>app.state.project?.id===project.id&&app.state.page==="mcp";
    const draw=()=>{
      const target=$("#page");if(!current()||!target)return;
      const focus=document.activeElement?.dataset.focus,selection=document.activeElement?.selectionStart;
      target.className="";target.innerHTML=component.page();
      target.onclick=async event=>{if(editorInteraction(event))return;const button=event.target.closest("[data-action]");if(!button||button.disabled)return;try{await component.action(button.dataset.action,button);}catch(error){notify(error.message,true);}};
      target.oninput=event=>component.input(event.target);
      target.onchange=event=>{try{if(editorInteraction(event))return;if(event.target.tagName==="SELECT"||event.target.type==="checkbox")component.input(event.target);component.change(event.target);}catch(error){notify(error.message,true);}};
      target.onsubmit=async event=>{event.preventDefault();try{await component.submit(event);}catch(error){notify(error.message,true);}};
      if(!mounted.has(target)){mounted.add(target);target.addEventListener("toggle",event=>{if(current())component.toggle(event.target);},{capture:true});}
      if(focus){const input=[...target.querySelectorAll("[data-focus]")].find(value=>value.dataset.focus===focus);if(input){input.focus({preventScroll:true});if(selection!=null&&["text","textarea"].includes(input.type))input.setSelectionRange(selection,selection);}}
    };
    const load=async(more=false)=>{const page=await rpc("mcp.list",{project_id:project.id,limit:25,...(more?{cursor:state.mcpCursor}:{})});state.mcpServers=more?[...state.mcpServers,...page.items.map(component.view)]:page.items.map(component.view);state.mcpCursor=page.cursor;if(app.state.project?.id===project.id){app.state.mcp=more?[...app.state.mcp,...page.items]:page.items;app.refreshFrame();}};
    const showRemove=(saved,after)=>{
      const content=element(`<div><p>移除 ${escape(saved.name)} 后，将停止接受新调用。历史、凭据与源目录会保留。</p><button class="btn danger" id="confirm-mcp-remove">移除服务</button></div>`),close=modal("移除 MCP 服务",content);
      $("#confirm-mcp-remove",content).onclick=async event=>{event.target.disabled=true;try{await rpc("mcp.remove",{project_id:project.id,server_id:saved.id,expected_revision:saved.revision});close();await after();notify("服务已移除，历史和源目录已保留。");}catch(error){notify(error.message,true);event.target.disabled=false;}};
    };
    const showImport=async()=>{
      const form=element('<form><p class="form-hint">只读取你选择的本机 TOML 文件。导入只保存选定服务，凭据写入本地秘密存储；连接测试由你另行发起。</p><label class="field">Codex 配置文件<input id="codex-config-path" required placeholder="config.toml 的绝对路径"></label><p><button class="btn" type="button" id="preview-codex-config">读取服务列表</button></p><div id="codex-import-list"></div><p><button class="btn primary" id="import-selected-codex" disabled>导入选定服务</button></p></form>'),close=modal("导入 Codex MCP 配置",form);
      let configPath="",cursor=null,items=[],selected=new Set();
      const drawCandidates=()=>{
        $("#codex-import-list",form).innerHTML=items.map(item=>`<label class="toggle-row"><span>${escape(item.name)}<small>${item.registered?"已注册":!item.importable?escape(item.error?.message||"需要手动配置"):item.transport==="stdio"?"本地进程":"HTTP 服务"}${item.has_credentials?" · 凭据不会回显":""}</small></span><input type="checkbox" data-codex-name="${escape(item.name)}" ${selected.has(item.name)?"checked":""} ${item.registered||!item.importable?"disabled":""}></label>`).join("")+(cursor!=null?'<button class="btn small" type="button" id="more-codex-candidates">加载更多服务</button>':"")+(items.length?"":'<p class="form-hint">文件中没有 MCP 服务。</p>');
        for(const input of form.querySelectorAll("[data-codex-name]"))input.onchange=()=>{input.checked?selected.add(input.dataset.codexName):selected.delete(input.dataset.codexName);$("#import-selected-codex",form).disabled=!selected.size;};
        if($("#more-codex-candidates",form))$("#more-codex-candidates",form).onclick=()=>preview(true);
        $("#import-selected-codex",form).disabled=!selected.size;
      };
      const preview=async(more=false)=>{try{const path=$("#codex-config-path",form).value.trim();if(more&&path!==configPath)throw new Error("配置路径已改变，请重新读取服务列表。");const page=await rpc("mcp.codex_candidates",{project_id:project.id,config_path:path,limit:25,...(more?{cursor}:{})});if(!more){selected.clear();configPath=path;}items=more?[...items,...page.items]:page.items;cursor=page.cursor;drawCandidates();}catch(error){notify(error.message,true);}};
      $("#preview-codex-config",form).onclick=()=>preview();
      $("#codex-config-path",form).oninput=()=>{$("#import-selected-codex",form).disabled=true;};
      form.onsubmit=async event=>{event.preventDefault();const button=$("#import-selected-codex",form);button.disabled=true;try{if(configPath!==$("#codex-config-path",form).value.trim())throw new Error("配置路径已改变，请重新读取服务列表。");const result=await rpc("mcp.import_codex",{project_id:project.id,config_path:configPath,server_names:[...selected]});await load();draw();drawCandidates();if(!result.errors.length)close();else $("#codex-import-list",form).insertAdjacentHTML("beforeend",result.errors.map(item=>`<p class="danger-text">${escape(item.name)}：${escape(item.error.message)}</p>`).join(""));notify(`已导入 ${result.imported.length} 个服务，跳过 ${result.skipped.length} 个，失败 ${result.errors.length} 个。`,!!result.errors.length);}catch(error){notify(error.message,true);}finally{button.disabled=!selected.size;}};
    };
    const showCredential=async(saved,after)=>{
      const target=saved.transport==="stdio"?"environment":"header";
      const form=element(`<form><p class="form-hint">保存后仅返回新的秘密引用，当前密钥值不回显。</p><label class="field">${target==="header"?"HTTP 头名称":"环境变量名"}<input id="mcp-secret-name" required value="${target==="header"?"Authorization":"API_KEY"}"></label><label class="field">新的认证值<input id="mcp-secret-value" type="password" autocomplete="new-password" required></label><p><button class="btn primary">保存认证</button></p></form>`),close=modal("设置 MCP 认证",form);
      form.onsubmit=async event=>{event.preventDefault();try{await rpc("mcp.set_credential",{server_id:saved.id,project_id:project.id,target,name:$("#mcp-secret-name",form).value,value:$("#mcp-secret-value",form).value,expected_revision:saved.revision});close();await after();notify("认证引用已更新，密钥值已保存在本地秘密存储。");}catch(error){notify(error.message,true);}};
    };
    const showSDK=async()=>modal("连接 Codex 与开发 SDK",element(`<div><p>使用 oea mcp 连接现有宿主。控制入口为 platform_guide 和 platform_call；业务工具仍需明确授权。</p><pre class="v2-pre">${escape('oea mcp --data-dir <external-state-directory>')}</pre><div class="markdown">${markdown('```python\nfrom oea import PlatformClient\n\nasync with PlatformClient.from_data_dir(data_dir) as client:\n    result = await client.call("host.status")\n```')}</div></div>`));
    component=createMCPDirectory({state,escape,icon,badge:value=>`<span class="badge ${escape(value)}">${escape(({CONNECTED:"已连接",CONNECTING:"连接中",DISCONNECTED:"未连接",SUCCEEDED:"完成",FAILED:"失败",UNCERTAIN:"待核对",RUNNING:"执行中",QUEUED:"已接受"})[value]||value)}</span>`,markdown,rpc,toast:notify,render:draw,loadBootstrap:load,showCredential,showSDK,showImport,showRemove,showToolTrial:(server,tool)=>showMCPTrial(app,project.id,server,tool,async()=>{await load();draw();}),time:date});
    entry={load,draw};projects.set(project.id,entry);
  }
  await entry.load();entry.draw();
}
