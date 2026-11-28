import test from "node:test";
import assert from "node:assert/strict";
import { registerHooks } from "node:module";

test("opening catalog sources reads the cache; adding, refreshing and removing are explicit",async t=>{
  let confirm=null;
  globalThis.__catalogConfirm=(title,message,action)=>{confirm={title,message,action};};
  const hook=registerHooks({load(url,context,next){
    if(url.endsWith("/ui.mjs"))return {format:"module",shortCircuit:true,source:`export * from ${JSON.stringify(url+"?source-test")};export const confirmRemoval=(...args)=>globalThis.__catalogConfirm(...args);`};
    return next(url,context);
  }});
  t.after(()=>{hook.deregister();delete globalThis.__catalogConfirm;});
  const {bindCatalogSources}=await import("../src/oea/web/static/plugin-settings.mjs");
  const calls=[],records=[],list={},save={disabled:false};
  let updates=0;
  const fields={source:{value:" catalog.json "},display:{value:" My catalog "}};
  const form={elements:fields,reset(){for(const field of Object.values(fields))field.value="";},querySelector:()=>save};
  const content={isConnected:true,querySelector:selector=>selector==="form"?form:list};
  const app={async rpc(operation,params){
    calls.push([operation,params]);
    if(operation==="plugins.sources")return {items:structuredClone(records)};
    if(operation==="plugins.add_source")records.push({id:"directory",...params,entry_count:0});
    if(operation==="plugins.refresh_source")records[0].entry_count=1;
    if(operation==="plugins.remove_source")records.length=0;
  }};
  await bindCatalogSources(app,content,async()=>{updates++;});
  assert.deepEqual(calls,[["plugins.sources",undefined]]);
  await form.onsubmit({preventDefault(){}});
  assert.deepEqual(calls[1],["plugins.add_source",{name:"My catalog",source:"catalog.json"}]);
  assert.match(list.innerHTML,/My catalog/);
  const press=attribute=>{
    const button={disabled:false,closest:selector=>selector==="button"?button:{dataset:{sourceIndex:"0"}},hasAttribute:key=>key===attribute};
    return list.onclick({target:button});
  };
  await press("data-refresh-source");
  assert.match(list.innerHTML,/1 个插件/);
  await press("data-remove-source");
  assert.equal(records.length,1);
  await confirm.action();
  assert.deepEqual(calls.filter(([op])=>op.endsWith("_source")).slice(1),[
    ["plugins.refresh_source",{source_id:"directory"}],["plugins.remove_source",{source_id:"directory"}],
  ]);
  assert.equal(updates,3);
  assert.match(list.innerHTML,/还没有自定义目录来源/);
});

test("plugin navigation exposes only installed plugins and standalone components",async()=>{
  const {pluginTabs}=await import("../src/oea/web/static/plugin-settings.mjs");
  assert.deepEqual(Object.keys(pluginTabs),["installed","mcp","skills"]);
});
