import assert from "node:assert/strict";
import test from "node:test";
import {createSkillIntegration} from "../src/oea/web/static/skill-directory.mjs";
import {createMCPDirectory} from "../src/oea/web/static/mcp-directory.mjs";
import {removeAgent} from "../src/oea/web/static/settings.mjs";

test("missing Skill sources can be deleted directly from the list after confirmation",async()=>{
  const saved={id:"guide",name:"Guide",description:"Evidence",enabled:true,revision:"3",directory:"/missing-source",bound_agents:["worker"]};
  const state={project:{id:"p"},skillRegistry:{items:[saved]},skillEpoch:0};
  const calls=[];let confirm;
  const component=createSkillIntegration({state,escape:String,icon:()=>"",markdown:String,toast:()=>{},render:()=>{},
    rpc:async(operation,params)=>{calls.push({operation,params});assert.equal(operation,"skills.remove");return {removed:true};},
    loadBootstrap:async()=>{state.skillRegistry={items:[]};},showRemove:(skill,after)=>{assert.equal(skill,saved);confirm=after;}});
  assert.match(component.page(),/data-action="registry-skill-delete" data-id="guide"/);
  assert.match(component.page(),/skill-card-title[\s\S]*registry-skill-select[\s\S]*class="badge[^\"]*">已启用/);
  assert.match(component.page(),/aria-label="删除 Guide"/);
  assert.doesNotMatch(component.page(),/>删除 Skill<\/button>/);
  await component.action("registry-skill-delete",{dataset:{id:"guide"}});
  assert.equal(calls.length,0);
  await confirm();
  assert.deepEqual(calls,[{operation:"skills.remove",params:{project_id:"p",skill_id:"guide",expected_revision:3}}]);
  assert.doesNotMatch(component.page(),/data-id="guide"/);
});

test("MCP list deletion uses saved metadata without connecting or opening details",async()=>{
  const saved={id:"service",name:"Service",description:"",enabled:true,status:"DISCONNECTED",revision:4,transport:"stdio",bound_agents:[],active_calls:0};
  const state={project:{id:"p"},mcpServers:[saved],mcpQuery:"",mcpDraft:null,mcpCursor:null};
  const calls=[];let confirm;
  const rpc=async(operation,params)=>{calls.push({operation,params});assert.equal(operation,"mcp.remove");return {removed:true};};
  const component=createMCPDirectory({state,escape:String,icon:()=>"",badge:String,markdown:String,rpc,toast:()=>{},render:()=>{},
    loadBootstrap:async()=>{state.mcpServers=[];},showRemove:(item,after)=>{assert.equal(item,saved);confirm=async()=>{await rpc("mcp.remove",{project_id:"p",server_id:item.id,expected_revision:item.revision});await after();};}});
  assert.match(component.page(),/data-action="mcp-remove" data-id="service"/);
  assert.match(component.page(),/mcp-card-actions[\s\S]*title="管理服务"[\s\S]*data-action="mcp-remove"/);
  await component.action("mcp-remove",{dataset:{id:"service"}});
  assert.equal(calls.length,0);
  await confirm();
  assert.deepEqual(calls,[{operation:"mcp.remove",params:{project_id:"p",server_id:"service",expected_revision:4}}]);
  assert.doesNotMatch(component.page(),/data-id="service"/);
});

test("Agent deletion uses the shared operation and preserves historical selections",async()=>{
  const state={project:{id:"p"},agents:[{definition:{id:"worker"}}],instances:[{id:"i"}],runs:[{id:"r"}],openTabs:["i"]};
  const calls=[];
  const app={state,rpc:async(operation,params)=>{calls.push({operation,params});assert.equal(operation,"agents.remove");return {removed:true};},reloadAgents:async()=>{state.agents=[];}};
  const history={instances:state.instances,runs:state.runs,openTabs:state.openTabs};
  assert.equal((await removeAgent(app,"worker",7)).removed,true);
  assert.deepEqual(calls,[{operation:"agents.remove",params:{agent_id:"worker",project_id:"p",expected_revision:7}}]);
  assert.deepEqual({instances:state.instances,runs:state.runs,openTabs:state.openTabs},history);
  assert.deepEqual(state.agents,[]);
});
