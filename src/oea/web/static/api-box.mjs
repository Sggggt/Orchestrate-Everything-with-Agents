import { $, element, escape, modal, notify, confirmRemoval } from "./ui.mjs";

import { actionSheet, libraryRow, searchControl, bindSearch } from "./library.mjs";
import { icon } from "./ui.mjs";

export async function listChoices(app, operation, params={}) {
  const items=[];
  let cursor=0;
  do {
    const page=await app.rpc(operation,{...params,cursor,limit:100});
    items.push(...page.items);
    cursor=page.cursor;
  } while(cursor!=null);
  return items;
}

export function modelOptions(items, selected, placeholder="请选择模型") {
  const options=[`<option value="">${escape(placeholder)}</option>`];
  if(selected&&!items.some(item=>item.id===selected)) options.push(`<option selected disabled value="${escape(selected)}">当前模型不可用 · ${escape(selected)}</option>`);
  for(const item of items) options.push(`<option value="${escape(item.id)}" ${item.id===selected?"selected":""}>${escape(item.name)}${item.model_id&&item.model_id!==item.name?" · "+escape(item.model_id):""}</option>`);
  return options.join("");
}

function editModel(app,item,refresh) {
  const advanced={...item?.model};
  delete advanced.base_url;
  delete advanced.model;
  const form=element(`<form><label class="field">模型名称<input name="name" required maxlength="80" value="${escape(item?.name)}"></label>
    <label class="field">上游模型 ID<input name="upstream" required value="${escape(item?.model_id)}"></label>
    <label class="field">API 地址<input name="endpoint" type="url" required value="${escape(item?.model?.base_url)}"></label>
    <label class="permission-toggle"><input name="enabled" type="checkbox" ${item?.enabled!==false?"checked":""}>启用此模型</label>
    <details><summary>生成参数、窗口和价格</summary><label class="field">模型参数 JSON<textarea name="advanced" rows="8">${escape(JSON.stringify(advanced,null,2))}</textarea></label></details>
    <footer class="dialog-footer"><button class="btn primary">保存模型</button></footer></form>`);
  const close=modal(item?"编辑模型":"添加模型",form);
  form.onsubmit=async event=>{
    event.preventDefault();
    const fields=form.elements, button=$("button",form);
    button.disabled=true;
    try {
      const model=JSON.parse(fields.advanced.value||"{}");
      if(!model||Array.isArray(model)||typeof model!=="object"||"credential_ref" in model)throw new Error("请填写非秘密模型参数对象。");
      await app.rpc("models.save",{...(item?{id:item.id,expected_revision:item.revision}:{}),name:fields.namedItem("name").value.trim(),model:{...model,model:fields.upstream.value.trim(),base_url:fields.endpoint.value.trim()},enabled:fields.enabled.checked});
      close();await refresh();
    }catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
}

function setCredential(app,item,refresh){
  const form=element('<form><label class="field">新密钥<input type="password" required autocomplete="new-password"></label><footer class="dialog-footer"><button class="btn primary">保存密钥</button></footer></form>');
  const close=modal("设置模型密钥",form);
  const input=$("input",form);
  close.onclose=()=>{input.value="";};
  form.onsubmit=async event=>{
    event.preventDefault();const button=$("button",form);button.disabled=true;
    try{await app.rpc("models.set_credential",{model_ref:item.id,value:input.value,expected_revision:item.revision});close();await refresh();}
    catch(error){notify(error.message,true);}finally{button.disabled=false;}
  };
}

function probe(app,item){
  const session=app.state.session;
  const form=element(`<form><label class="field">探测使用的 Agent<select required>${app.state.agents.map(row=>`<option value="${escape(row.definition.id)}">${escape(row.definition.name)}</option>`).join("")}</select></label><p>会话 · ${escape(session.name)}</p><div role="status"></div><footer class="dialog-footer"><button class="btn primary">测试连接</button></footer></form>`);
  modal("测试模型连接",form);
  const key=crypto.randomUUID();let run=null;
  form.onsubmit=async event=>{
    event.preventDefault();
    const button=$("button",form),agent=$("select",form),status=$("[role=status]",form);button.disabled=true;agent.disabled=true;
    try{
      run??=await app.rpc("models.probe",{model_ref:item.id,agent_id:agent.value,session_id:session.id,idempotency_key:key});
      status.textContent="已提交探测 · "+run.id;
      await app.rpc("runs.result",{run_id:run.id,timeout:30});
      status.textContent="模型已响应 · "+run.id;button.hidden=true;
    }catch(error){status.textContent=error.message;button.textContent=run?"查看探测结果":"重试";}finally{button.disabled=false;}
  };
}

export async function renderBox(app,root){
  let rows=[],query="",request=0;
  const current=()=>root.isConnected&&app.state.page==="api-box"&&app.onBoxEvent===refresh;
  root.className="library-page";
  root.innerHTML=`<section class="library"><header class="library-header"><h1>API Box</h1><div class="library-tools">${searchControl("搜索模型")}<button type="button" class="btn primary" data-add>${icon("plus")}添加模型</button></div></header><div class="library-grid" data-model-list></div></section>`;
  const list=$("[data-model-list]",root);
  let cursor=null;
  function draw(){
    list.innerHTML=rows.map((item,index)=>item.name.toLocaleLowerCase().includes(query)||item.model_id.toLocaleLowerCase().includes(query)?
      libraryRow({name:item.name,symbol:"chip",index,state:item.enabled?(item.configured?"已配置":"待配置"):"已停用",active:item.enabled&&item.configured,openLabel:"编辑 "+item.name}):"").join("")
      ||`<div class="library-empty">${icon("chip")}<h2>${query?"没有匹配的模型":"暂无模型"}</h2></div>`;
    if(cursor!=null)list.insertAdjacentHTML("beforeend",'<button class="text-link library-next" data-more>加载更多</button>');
  }
  async function load(more=false){
    const ticket=++request;
    let next=more?cursor:0;
    const found=[];
    do{
      const page=await app.rpc("models.list",{include_disabled:true,cursor:next,limit:100});
      if(!current()||ticket!==request)return;
      found.push(...page.items);next=page.cursor;
    }while(query && next!=null);
    rows=more?[...rows,...found]:found;cursor=next;draw();
  }
  function refresh(){return load();}
  bindSearch(root,value=>{query=value;draw();if(query&&cursor!=null)load(true).catch(error=>notify(error.message,true));});
  root.onclick=event=>{
    const button=event.target.closest("button");if(!button)return;
    const item=rows[button.closest("[data-row]")?.dataset.row];
    if(button.hasAttribute("data-add"))editModel(app,null,refresh);
    if(button.hasAttribute("data-more"))load(true).catch(error=>notify(error.message,true));
    if(button.hasAttribute("data-open"))editModel(app,item,refresh);
    if(button.hasAttribute("data-options"))actionSheet(item.name,[
      {label:"编辑模型",symbol:"edit",run:()=>editModel(app,item,refresh)},
      {label:"设置密钥",symbol:"lock",run:()=>setCredential(app,item,refresh)},
      {label:"测试连接",symbol:"activity",disabled:!item.enabled||!app.state.session||!app.state.agents.length,run:()=>probe(app,item)},
      {label:"删除模型",symbol:"trash",danger:true,run:()=>confirmRemoval("删除模型",`删除 ${item.name} 后，现有引用需要重新选择。`,async()=>{await app.rpc("models.remove",{model_ref:item.id,expected_revision:item.revision});await refresh();})}
    ]);
  };
  app.onBoxEvent=refresh;
  await refresh();
}
