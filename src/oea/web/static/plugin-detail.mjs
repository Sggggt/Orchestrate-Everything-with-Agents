import { $, element, escape, icon, modal, notify, confirmRemoval } from "./ui.mjs";
import { glyph, pluginName } from "./library.mjs";
import { markdown } from "./markdown.mjs";

function updateDialog(app, plugin, refresh) {
  const form=element(`<form><label class="field">插件来源<input name="source" required value="${escape(plugin.source)}"></label>
    <footer class="dialog-footer"><button class="btn primary" type="submit">更新插件</button></footer></form>`);
  const close=modal("更新插件",form);
  let previousSource, key;
  form.onsubmit=async event=>{
    event.preventDefault();
    const button=$("button",form), source=form.elements.source.value.trim();
    if(button.disabled||!source)return;
    if(previousSource!==source){key=crypto.randomUUID();previousSource=source;}
    button.disabled=true;
    try{
      await app.rpc("plugins.update",{plugin_id:plugin.id,source,expected_revision:plugin.revision,idempotency_key:key});
      await close();
      await refresh();
    }catch(error){notify(error.message,true);}
    finally{button.disabled=false;}
  };
}

async function readSkill(app,component,projectId,active){
  let offset=0,revision;
  const text=[];
  do{
    const page=await app.rpc("skills.read",{project_id:projectId,skill_id:component.skill_id,offset,limit:16384,...(revision?{revision}:{})});
    if(!active())return;
    text.push(page.text);
    revision=page.revision;
    offset=page.next_offset;
  }while(offset!=null);
  modal(component.skill_id,element(`<article class="markdown">${markdown(text.join(""))}</article>`));
}

function componentsSection(plugin,projectId){
  const components=plugin.components||[];
  if(!components.length)return "";
  const items=components.map((component,index)=>{
    const isMCP=component.kind==="mcp";
    const name=component.definition?.name||component.name||component.skill_id||component.server_id;
    return `<li class="plugin-component">
      ${glyph(name,isMCP?"terminal":"book",isMCP?"ocean":"ochre")}
      <span class="plugin-component-name"><strong>${escape(name)}</strong><small>${isMCP?"MCP":"Skill"} · ${component.ownership==="reference"?"引用":"插件专属"}</small></span>
      ${isMCP&&component.definition?`<button class="btn" data-edit="${index}">编辑连接</button>`:!isMCP&&projectId?`<button class="btn" data-read="${index}">查看</button>`:""}
    </li>`;
  });
  return `<section class="plugin-settings-section"><h2>组件</h2><ul class="plugin-components">${items.join("")}</ul></section>`;
}

// Both entry points use this page and the same revision-checked platform operations.
export async function renderPluginDetail(app,root,{editComponent,buildPlugin}){
  const projectId=app.state.project?.id||null, pluginId=app.state.pluginId;
  root.innerHTML=`<section class="library plugin-detail" aria-label="插件设置">
    <button class="text-link plugin-back" data-back>${icon("chevron")}返回插件</button>
    <div data-plugin-settings aria-busy="true"></div></section>`;
  const panel=$("[data-plugin-settings]",root);
  $("[data-back]",root).onclick=app.leavePluginSettings;
  const active=()=>panel.isConnected&&app.state.page==="plugins"&&app.state.pluginId===pluginId&&(app.state.project?.id||null)===projectId;
  let sequence=0,plugin=null,saving=false;
  async function refresh(){
    if(!active())return;
    const request=++sequence;
    try{
      const saved=await app.rpc("plugins.get",{plugin_id:pluginId,project_id:projectId});
      if(!active()||request!==sequence)return;
      if(saved.removed){
        plugin=null;
        panel.innerHTML='<h1>插件已移除</h1>';
        return;
      }
      plugin=saved;
      const name=pluginName(plugin);
      const issues=plugin.manifest?.extensions?.["org.oea"]?.issues||[];
      panel.innerHTML=`<header class="library-header"><div class="plugin-identity">${glyph(name,plugin.id==="agent-factory"?"agents":"cube",plugin.id==="agent-factory"?"jade":"iris")}
        <div><h1>${escape(name)}</h1><small>${plugin.official?"官方 · ":""}${escape(plugin.version)}</small></div></div>
        <button type="button" class="plugin-switch" role="switch" aria-label="启用插件" aria-checked="${plugin.enabled}" data-toggle>
          <span aria-hidden="true"></span>${plugin.enabled?"已启用":"已停用"}</button></header>
        ${issues.map(issue=>`<p class="danger-text" role="status">${escape(issue.component||issue.field||"插件")} · ${escape(issue.error?.safe_message||issue.error?.code||issue.reason||"组件不可用")}</p>`).join("")}
        ${componentsSection(plugin,projectId)}
        ${(plugin.operations||[]).length?`<section class="plugin-settings-section"><h2>平台操作</h2><div class="plugin-operations">${plugin.operations.map(op=>`<code>${escape(op)}</code>`).join("")}</div></section>`:""}
        <div class="plugin-settings-actions">${plugin.id==="plugin-creator"&&plugin.enabled&&projectId?'<button class="btn primary" data-build>创建插件包</button>':""}
        ${plugin.official?"":'<button class="btn" data-update>更新插件</button><button class="btn danger-text" data-remove>移除插件</button>'}</div>`;
    }catch(error){
      if(active()&&request===sequence){
        plugin=null;
        panel.innerHTML=`<div role="alert">${escape(error.message)}</div><button class="btn" data-retry>重新读取</button>`;
      }
    }finally{
      if(active()&&request===sequence)panel.setAttribute("aria-busy","false");
    }
  }
  panel.onclick=async event=>{
    const button=event.target.closest("button");
    if(!button||button.disabled||!active())return;
    if(button.hasAttribute("data-retry")){await refresh();return;}
    if(!plugin||saving)return;
    const saved=plugin;
    try{
      if(button.hasAttribute("data-toggle")){
        saving=true;button.disabled=true;
        await app.rpc("plugins.set_enabled",{plugin_id:saved.id,enabled:!saved.enabled,expected_revision:saved.revision});
        await refresh();
      }else if(button.hasAttribute("data-edit")){
        editComponent(app,saved,saved.components[Number(button.dataset.edit)],refresh);
      }else if(button.hasAttribute("data-read")){
        button.disabled=true;
        await readSkill(app,saved.components[Number(button.dataset.read)],projectId,active);
      }else if(button.hasAttribute("data-build")){
        await buildPlugin(app,projectId);
      }else if(button.hasAttribute("data-update")){
        updateDialog(app,saved,refresh);
      }else if(button.hasAttribute("data-remove")){
        confirmRemoval("移除插件",pluginName(saved),async()=>{
          await app.rpc("plugins.remove",{plugin_id:saved.id,expected_revision:saved.revision});
          if(active())await app.leavePluginSettings();
        });
      }
    }catch(error){notify(error.message,true);}
    finally{saving=false;button.disabled=false;}
  };
  app.onPluginEvent=fact=>{if(!fact||fact.data?.id===pluginId)return refresh();};
  await refresh();
}
