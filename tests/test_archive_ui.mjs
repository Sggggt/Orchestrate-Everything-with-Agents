import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

// Exercise the real application and click handlers with deterministic RPC and
// DOM boundaries. Layout and browser interaction are verified separately.
class Node {
  constructor(html="",dataset={}){this.innerHTML=html;this.dataset=dataset;this.value="";this.nodes=new Map();this.isConnected=true;this.parentElement={scrollTop:0};}
  querySelector(selector){
    if(!this.nodes.has(selector))this.nodes.set(selector,new Node());
    return this.nodes.get(selector);
  }
  querySelectorAll(selector){
    const attribute=selector.match(/^\[(data-[\w-]+)\]$/)?.[1];
    if(!attribute)return [];
    const result=[];
    for(const match of this.innerHTML.matchAll(/<button\b[^>]*>/g)){
      const value=match[0].match(new RegExp(`${attribute}="([^"]*)"`))?.[1];
      if(value===undefined)continue;
      const key=attribute+":"+value;
      if(!this.nodes.has(key))this.nodes.set(key,new Node("",{[attribute.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:value}));
      result.push(this.nodes.get(key));
    }
    return result;
  }
  focus(){}
  replaceWith(){}
}

test("active sessions, paginated archive restoration and confirmed project deletion stay in sync",async t=>{
  const globals=["window","document","location","history","localStorage","EventSource","__archiveUI"];
  const previous=new Map(globals.map(key=>[key,globalThis[key]]));
  const roots=new Map(["#app","#page","#project-select","#workspace-select","#connection"].map(key=>[key,new Node()]));
  const dialogs=[],calls=[],notifications=[],streams=[],deletedReceipts=new Map(),storage=new Map([["oea-project","p"],["oea-session:p","old"]]);
  const projects=[{id:"p",name:"Project"},{id:"q",name:"Other project"}];
  const sessions=[{id:"old",name:"Old archived session",project_id:"p",archived:1,position:1},
    {id:"a",name:"First session",project_id:"p",archived:0,position:2},
    {id:"b",name:"Next session",project_id:"p",archived:0,position:3}];
  const ui={
    $(selector,root){if(root)return root.querySelector(selector);if(selector.includes("dialog-backdrop"))return dialogs.at(-1)?.content||null;return roots.get(selector)||null;},
    element:html=>new Node(html),
    modal(title,content){const dialog={title,content};dialogs.push(dialog);const close=()=>{close.onclose?.();dialogs.splice(dialogs.indexOf(dialog),1);};dialog.close=close;return close;},
    notify:(message,error)=>notifications.push({message,error}),
    async rpc(operation,params={}){
      calls.push({operation,params:structuredClone(params)});
      let result;
      if(operation==="agents.schema")result={};
      else if(operation==="host.status")result={version:"2",data_dir:"<platform-state>"};
      else if(operation==="projects.list")result={items:projects,cursor:null};
      else if(operation==="sessions.list"){
        const values=sessions.filter(value=>value.project_id===params.project_id&&(params.archived===undefined||!!value.archived===params.archived)&&value.position>(params.cursor||0));
        const items=values.slice(0,params.limit);result={items,cursor:values.length>params.limit?items.at(-1).position:null};
      }
      else if(operation==="sessions.get")result=sessions.find(value=>value.id===params.session_id);
      else if(operation==="sessions.set_archived"){result=sessions.find(value=>value.id===params.session_id);result.archived=Number(params.archived);}
      else if(operation==="sessions.summary")result={runs:{},artifacts:0};
      else if(operation==="instances.list")result={items:[{id:"instance-"+params.session_id,name:"Conversation",agent_id:"worker",session_id:params.session_id}],cursor:null};
      else if(operation==="projects.delete"){result=deletedReceipts.get(params.idempotency_key);if(!result){const index=projects.findIndex(value=>value.id===params.project_id);assert.equal(params.confirm_name,projects[index].name);projects.splice(index,1);result={deleted:true};deletedReceipts.set(params.idempotency_key,result);}}
      else if(["agents.list","mcp.list","skills.list","runs.list","artifacts.list"].includes(operation))result={items:[],cursor:null};
      else throw new Error("Unexpected operation: "+operation);
      return structuredClone(result);
    },
  };
  globalThis.__archiveUI=ui;
  globalThis.window={addEventListener(){}};
  globalThis.document={querySelector:selector=>ui.$(selector),activeElement:null};
  globalThis.location={hash:"#workspace"};globalThis.history={replaceState(){}};
  globalThis.localStorage={getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,String(value)),removeItem:key=>storage.delete(key)};
  globalThis.EventSource=class{constructor(){streams.push(this);}close(){this.closed=true;}};
  const hooks=registerHooks({load(url,context,nextLoad){
    if(url.endsWith("/ui.mjs"))return {format:"module",shortCircuit:true,source:`export * from ${JSON.stringify(url+"?original")};export const $=(...args)=>globalThis.__archiveUI.$(...args),rpc=(...args)=>globalThis.__archiveUI.rpc(...args),element=(...args)=>globalThis.__archiveUI.element(...args),modal=(...args)=>globalThis.__archiveUI.modal(...args),notify=(...args)=>globalThis.__archiveUI.notify(...args);`};
    return nextLoad(url,context);
  }});
  t.after(()=>{hooks.deregister();for(const [key,value] of previous)if(value===undefined)delete globalThis[key];else globalThis[key]=value;});
  const {app}=await import("../src/oea/web/static/app.mjs");
  const settle=async condition=>{for(let index=0;index<50&&!condition();index++)await new Promise(resolve=>setImmediate(resolve));assert.ok(condition());};
  const action=name=>roots.get("#app").querySelectorAll("[data-action]").find(button=>button.dataset.action===name).onclick();
  await settle(()=>app.state.session?.id==="a"&&streams.length>0);
  assert.deepEqual(app.state.sessions.map(value=>value.id),["a","b"]);
  assert.equal(storage.get("oea-session:p"),"a");
  assert.doesNotMatch(roots.get("#app").innerHTML,/Old archived session/);
  assert.equal(calls.find(value=>value.operation==="sessions.list").params.archived,false);

  await t.test("archiving the selected session clears its open conversation and selects the next active session",async()=>{
    app.state.page="conversation";app.state.openTabs=["instance-a"];app.state.selectedInstance=app.state.instances[0];app.refreshFrame();
    action("session-settings");await dialogs.at(-1).content.querySelector("#archive-session").onclick();
    assert.equal(app.state.session.id,"b");assert.equal(app.state.page,"overview");
    assert.deepEqual(app.state.openTabs,[]);assert.equal(app.state.selectedInstance,null);
    assert.equal(storage.get("oea-session:p"),"b");
    assert.doesNotMatch(roots.get("#app").innerHTML,/First session|instance-a/);
  });
  await t.test("archiving the last active session clears selection and session content",async()=>{
    action("session-settings");await dialogs.at(-1).content.querySelector("#archive-session").onclick();
    assert.equal(app.state.session,null);assert.deepEqual(app.state.sessions,[]);
    assert.deepEqual(app.state.instances,[]);assert.deepEqual(app.state.runs,[]);assert.deepEqual(app.state.artifacts,[]);
    assert.equal(app.state.artifactCursor,null);assert.equal(storage.has("oea-session:p"),false);
  });
  await t.test("archive reads its own server pages and restores an item absent from active cache",async()=>{
    for(let index=0;index<27;index++)sessions.push({id:"archive-"+index,name:"Archived "+index,project_id:"p",archived:1,position:index+4});
    await action("archive");const dialog=dialogs.at(-1);
    assert.equal(dialog.content.querySelectorAll("[data-restore-session]").length,25);
    assert.deepEqual(calls.filter(value=>value.operation==="sessions.list"&&value.params.archived).at(-1).params,{project_id:"p",archived:true,cursor:0,limit:25});
    assert.doesNotMatch(dialog.content.innerHTML,/恢复项目/);
    await dialog.content.querySelector("#more-archive").onclick();
    assert.equal(dialog.content.querySelectorAll("[data-restore-session]").length,30);
    const restore=dialog.content.querySelectorAll("[data-restore-session]").find(button=>button.dataset.restoreSession==="archive-26");
    await restore.onclick();
    assert.equal(app.state.session.id,"archive-26");
    assert.ok(app.state.sessions.some(value=>value.id==="archive-26"));
    assert.doesNotMatch(dialog.content.innerHTML,/data-restore-session="archive-26"/);
    dialog.close();
  });
  await t.test("quick jump excludes stale archived cache entries",async()=>{
    app.state.sessions.push(structuredClone(sessions[0]));action("search");
    const dialog=dialogs.at(-1),html=dialog.content.querySelector("#quick-results").innerHTML;
    assert.doesNotMatch(html,/Old archived session/);assert.match(html,/Archived 26/);dialog.close();
    app.state.sessions=app.state.sessions.filter(value=>!value.archived);
  });
  await t.test("a late instance response cannot repopulate an archived session",async()=>{
    const originalRPC=ui.rpc;let release;const gate=new Promise(resolve=>{release=resolve;});
    ui.rpc=async(operation,params)=>{if(operation==="instances.list")await gate;return originalRPC(operation,params);};
    const pending=app.reloadInstances();
    try{action("session-settings");await dialogs.at(-1).content.querySelector("#archive-session").onclick();}
    finally{release();await pending;ui.rpc=originalRPC;}
    assert.equal(app.state.session,null);assert.deepEqual(app.state.instances,[]);
    const restored=await ui.rpc("sessions.set_archived",{project_id:"p",session_id:"archive-26",archived:false});
    await app.reloadSessions({restored});app.refreshFrame();
  });
  await t.test("an external archive event removes the current session and its tabs",async()=>{
    sessions.find(value=>value.id==="archive-26").archived=1;
    app.state.openTabs=["instance-archive-26"];app.state.selectedInstance=app.state.instances[0];
    streams.at(-1).onmessage({data:JSON.stringify({kind:"event",position:1,event:{kind:"session.archived",session_id:"archive-26"}})});
    await settle(()=>app.state.session===null&&app.state.openTabs.length===0);
    assert.deepEqual(app.state.sessions,[]);
  });
  await t.test("external registration deletion events refresh Agent, MCP and Skill directories",async()=>{
    app.state.page="overview";
    for(const [field,kind,data] of [["agents","agent.removed",{agent_id:"gone"}],["mcp","mcp.saved",{server_id:"gone",removed:true}],["skills","skill.removed",{skill_id:"gone"}]]){
      app.state[field]=[{definition:{id:"gone"}}];
      streams.at(-1).onmessage({data:JSON.stringify({kind:"event",position:2,event:{kind,project_id:"p",data}})});
      await settle(()=>app.state[field].length===0);
      assert.ok(calls.some(value=>value.operation===({agents:"agents.list",mcp:"mcp.list",skills:"skills.list"})[field]));
    }
  });
  await t.test("project deletion requires the exact name and clears the deleted scope",async()=>{
    storage.set("oea-session:p","old");storage.set("oea-events:p","10");app.state.openTabs=["old-tab"];
    action("project-settings");const settings=dialogs.at(-1);assert.doesNotMatch(settings.content.innerHTML,/归档项目|恢复项目/);
    settings.content.querySelector("#delete-project").onclick();const dialog=dialogs.at(-1);
    assert.match(dialog.content.innerHTML,/无法恢复/);assert.match(dialog.content.innerHTML,/原件目录和外部备份会保留/);
    const input=dialog.content.querySelector("#delete-project-name");input.value="Incorrect";
    await dialog.content.onsubmit({preventDefault(){}});assert.equal(calls.some(value=>value.operation==="projects.delete"),false);
    const originalRPC=ui.rpc;let lost=false;
    ui.rpc=async(operation,params)=>{const result=await originalRPC(operation,params);if(operation==="projects.delete"&&!lost){lost=true;throw new Error("Simulated lost response");}return result;};
    input.value="Project";
    try{await dialog.content.onsubmit({preventDefault(){}});assert.equal(app.state.project.id,"p");await dialog.content.onsubmit({preventDefault(){}});}
    finally{ui.rpc=originalRPC;}
    const request=calls.find(value=>value.operation==="projects.delete");assert.equal(request.params.confirm_name,"Project");assert.ok(request.params.idempotency_key);
    assert.equal(calls.filter(value=>value.operation==="projects.delete")[1].params.idempotency_key,request.params.idempotency_key);
    assert.equal(app.state.project.id,"q");assert.deepEqual(app.state.openTabs,[]);
    assert.equal(storage.has("oea-session:p"),false);assert.equal(storage.has("oea-events:p"),false);
    assert.equal(storage.get("oea-project"),"q");assert.equal(calls.some(value=>value.operation==="projects.set_archived"),false);
  });
  await t.test("deleting the last project leaves a clean project creation state",async()=>{
    action("project-settings");dialogs.at(-1).content.querySelector("#delete-project").onclick();const dialog=dialogs.at(-1);
    dialog.content.querySelector("#delete-project-name").value="Other project";await dialog.content.onsubmit({preventDefault(){}});
    assert.equal(app.state.project,null);assert.equal(app.state.session,null);assert.deepEqual(app.state.instances,[]);
    assert.equal(storage.has("oea-project"),false);assert.match(roots.get("#page").innerHTML,/创建你的第一个项目/);
  });
});
