import { $, element, escape, icon, modal, notify, confirmRemoval } from "./ui.mjs";
import { renderPluginDetail } from "./plugin-detail.mjs";
import { manageMCP } from "./mcp-settings.mjs";
import { manageSkills } from "./skill-settings.mjs";
import { listChoices } from "./api-box.mjs";

import { actionSheet, libraryRow, searchControl, bindSearch, pluginName } from "./library.mjs";

export const pluginTabs={installed:"已安装",mcp:"MCP",skills:"Skills"};


export async function bindCatalogSources(app,content,onChange){
  const form=$("form",content),list=$("[data-source-list]",content);
  let sequence=0;
  async function load(){
    const request=++sequence;
    const {items}=await app.rpc("plugins.sources");
    if(request!==sequence||!content.isConnected)return;
    list.innerHTML=items.map((source,index)=>`<article class="v2-panel" data-source-index="${index}">
        <div class="section-heading"><h3>${escape(source.name)}</h3><small>${escape(source.entry_count)} 个插件</small></div>
        <p class="v2-muted">${escape(source.source)}</p><div class="v2-actions">
        <button class="btn" type="button" data-refresh-source>刷新</button>
        <button class="btn danger-text" type="button" data-remove-source>移除</button></div></article>`).join("")||'<p class="quiet-state">还没有自定义目录来源。</p>';
    list.onclick=async event=>{
      const button=event.target.closest("button");
      if(!button||button.disabled)return;
      const source=items[button.closest("[data-source-index]").dataset.sourceIndex];
      if(button.hasAttribute("data-remove-source")){
        confirmRemoval("移除目录来源",`移除 ${source.name} 后，已安装的插件将保留。`,async()=>{
          await app.rpc("plugins.remove_source",{source_id:source.id});await changed();
        });
      }else if(button.hasAttribute("data-refresh-source")){
        button.disabled=true;
        try{await app.rpc("plugins.refresh_source",{source_id:source.id});await changed();}
        catch(error){notify(error.message,true);}finally{button.disabled=false;}
      }
    };
  }
  async function changed(){await Promise.all([load(),onChange()]);}
  form.onsubmit=async event=>{
    event.preventDefault();
    const button=$("button",form);if(button.disabled)return;
    button.disabled=true;
    try{
      await app.rpc("plugins.add_source",{name:form.elements.display.value.trim(),source:form.elements.source.value.trim()});
      form.reset();await changed();
    }catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
  await load();
}

async function manageCatalogSources(app,refresh){
  const content=element(`<div><form><label class="field">目录名称<input name="display" required></label>
      <label class="field">目录 JSON 文件路径或 HTTPS 地址<input name="source" required></label>
      <footer class="dialog-footer"><button class="btn primary">添加目录来源</button></footer></form>
      <section data-source-list aria-label="已登记的目录来源"></section></div>`);
  modal("目录来源",content);
  await bindCatalogSources(app,content,refresh);
}

function mutationForm(title,body,submit,after,submitLabel="保存"){
  const form=element(`<form>${body}<footer class="dialog-footer"><button class="btn primary">${escape(submitLabel)}</button></footer></form>`);
  const close=modal(title,form);
  form.onsubmit=async event=>{
    event.preventDefault();const button=$("button",form);button.disabled=true;
    try{const result=await submit(form);close();await after(result);}catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
  return form;
}

function editComponent(app,plugin,component,refresh){
  const def=component.definition;
  if(!def)throw new Error("尚未取得 MCP 连接定义。");
  const body=`<p>${component.ownership==="reference"?"保存后将创建此插件专用的连接副本。":"正在编辑此插件的专用连接。"}</p>
    <label class="field">名称<input name="display" required value="${escape(def.name)}"></label>`+
    (def.transport==="stdio"?
      `<label class="field">启动命令与参数（JSON 数组）<textarea name="command" required>${escape(JSON.stringify(def.command))}</textarea></label>`:
      `<label class="field">服务地址<input name="url" required type="url" value="${escape(def.url)}"></label>`);
  mutationForm("插件 MCP 连接",body,form=>{
    const definition={...def,name:form.elements.display.value};
    if(def.transport==="stdio")definition.command=JSON.parse(form.elements.command.value);
    else definition.url=form.elements.url.value;
    return app.rpc("plugins.configure_component",{plugin_id:plugin.id,component_id:component.id,definition,expected_revision:plugin.revision});
  },refresh);
}

async function buildPlugin(app,projectId){
  const [servers,skills]=await Promise.all(["mcp.list","skills.list"].map(op=>listChoices(app,op,{project_id:projectId})));
  const inputs=(records,name)=>records.map(({definition})=>`<label class="permission-toggle"><input type="checkbox" name="${name}" value="${escape(definition.id)}">${escape(definition.name)}</label>`).join("")||"暂无登记";
  let key=crypto.randomUUID(),last="";
  mutationForm("创建插件包",`<label class="field">插件名称<input name="pluginName" required></label><fieldset><legend>MCP</legend>${inputs(servers,"servers")}</fieldset><fieldset><legend>Skills</legend>${inputs(skills,"skills")}</fieldset>`,form=>{
    const checked=name=>[...form.querySelectorAll(`[name=${name}]:checked`)].map(node=>node.value);
    const params={project_id:projectId,name:form.elements.pluginName.value.trim(),server_ids:checked("servers"),skill_ids:checked("skills")};
    const next=JSON.stringify(params);if(next!==last)key=crypto.randomUUID();last=next;
    return app.rpc("plugins.build",{...params,idempotency_key:key});
  },result=>modal("插件包已创建",element(`<pre class="v2-pre">${escape(result.path||result.source||JSON.stringify(result,null,2))}</pre>`)));
}

export async function renderPlugins(app,root){
  const tab=Object.hasOwn(pluginTabs,app.state.pluginTab)?app.state.pluginTab:"installed",projectId=app.state.project?.id||null;
  root.className="library-page";
  if(tab==="installed"&&app.state.pluginId){
    return renderPluginDetail(app,root,{editComponent,buildPlugin});
  }
  root.innerHTML=`<section class="library"><header class="library-header"><h1>插件</h1>${tab==="installed"?`<div class="library-tools">${searchControl("搜索插件")}<button type="button" class="icon-btn" data-sources title="目录来源" aria-label="目录来源">${icon("settings")}</button><button type="button" class="btn primary" data-path>${icon("plus")}安装插件</button></div>`:""}</header>
    <nav class="v2-tabs library-tabs" aria-label="插件分类">${Object.entries(pluginTabs).map(([id,title])=>`<button class="${id===tab?"active":""}" data-plugin-tab="${id}" aria-current="${id===tab?"page":"false"}">${title}</button>`).join("")}</nav><div data-plugin-content></div></section>`;
  for(const button of root.querySelectorAll("[data-plugin-tab]"))button.onclick=()=>app.navigate("plugins",{pluginTab:button.dataset.pluginTab});
  const panel=$("[data-plugin-content]",root);
  const active=()=>panel.isConnected&&app.state.page==="plugins"&&app.state.pluginTab===tab&&(app.state.project?.id||null)===projectId;
  if(tab==="mcp"||tab==="skills"){
    if(!projectId){panel.textContent="请选择项目";return;}
    await (tab==="mcp"?manageMCP:manageSkills)(app,panel);return;
  }
  let rows=[],cursor=null,request=0,query="";
  const installKeys=new Map();
  async function install(source){
    if(!installKeys.has(source))installKeys.set(source,crypto.randomUUID());
    await app.rpc("plugins.install",{source,project_id:projectId,idempotency_key:installKeys.get(source)});
    installKeys.delete(source);await refresh();
  }
  async function refresh(more=false){
    const serial=++request;
    const found=[];
    let next=more?cursor:0;
    do{
      const page=await app.rpc("plugins.list",{project_id:projectId,cursor:next,limit:25});
      if(!active()||serial!==request)return;
      found.push(...page.items);next=page.cursor;
    }while(query&&next!=null);
    rows=more?[...rows,...found]:found;cursor=next;draw();
  }
  function draw(){
    panel.innerHTML=`<div class="library-grid">${rows.map((item,index)=>{
      if(![pluginName(item),item.name,item.id].some(text=>text.toLocaleLowerCase().includes(query)))return "";
      return libraryRow({index,name:pluginName(item),symbol:item.id==="agent-factory"?"agents":"cube",tone:item.id==="agent-factory"?"jade":"iris",active:item.enabled,openLabel:"打开"+pluginName(item)+"设置",state:item.enabled?"已启用":"已停用"});
    }).join("")||`<div class="library-empty">${icon("plug")}<h2>${query?"没有匹配的插件":"暂无插件"}</h2></div>`}</div>
    ${cursor!=null?'<button class="text-link library-next" data-more>加载更多</button>':""}`;
  }
  bindSearch(root,value=>{query=value;draw();if(query&&cursor!=null)refresh(true).catch(error=>notify(error.message,true));});
  $("[data-sources]",root).onclick=()=>manageCatalogSources(app,refresh).catch(error=>notify(error.message,true));
  $("[data-path]",root).onclick=async()=>{
    const form=mutationForm("安装插件",'<label class="field">插件来源<input name="source" list="install-sources" placeholder="目录、ZIP 或 Git 地址" required></label><datalist id="install-sources"></datalist>',form=>install(form.elements.source.value.trim()),()=>{},"安装");
    try{
      const entries=await listChoices(app,"plugins.catalog");
      if(form.isConnected)$("datalist",form).innerHTML=entries.filter(item=>!item.installed).map(item=>`<option value="${escape(item.source)}">${escape(pluginName(item))}</option>`).join("");
    }catch(error){if(form.isConnected)notify(error.message,true);}
  };
  const openSettings=item=>app.navigate("plugins",{pluginId:item.id});
  panel.onclick=async event=>{
    const b=event.target.closest("button");if(!b||b.disabled)return;
    const item=rows[b.closest("[data-row]")?.dataset.row];
    b.disabled=true;
    try{
      if(b.hasAttribute("data-more"))await refresh(true);
      if(b.hasAttribute("data-open"))await openSettings(item);
      if(b.hasAttribute("data-options")){
        const actions=[
          {label:"插件设置",symbol:"settings",run:()=>openSettings(item)},
          {label:item.enabled?"停用":"启用",symbol:"power",run:async()=>{await app.rpc("plugins.set_enabled",{plugin_id:item.id,enabled:!item.enabled,expected_revision:item.revision});await refresh();}}
        ];
        if(item.id==="plugin-creator"&&item.enabled&&projectId)actions.unshift({label:"创建插件包",symbol:"cube",run:()=>buildPlugin(app,projectId)});
        if(!item.official)actions.push({label:"移除插件",symbol:"trash",danger:true,run:()=>confirmRemoval("移除插件",pluginName(item),async()=>{await app.rpc("plugins.remove",{plugin_id:item.id,expected_revision:item.revision});await refresh();})});
        actionSheet(pluginName(item),actions);
      }
    }catch(error){notify(error.message,true);}finally{b.disabled=false;}
  };
  app.onPluginEvent=()=>active()&&refresh();await refresh();
}
