import test from "node:test";
import assert from "node:assert/strict";
import { createMCPDirectory } from "../src/oea/web/static/mcp-directory.mjs";
import { createSkillIntegration } from "../src/oea/web/static/skill-directory.mjs";

test("editing MCP display metadata preserves saved argv and secret references through the v2 contract",async t=>{
  const previous=globalThis.document;globalThis.document={querySelector:()=>null};t.after(()=>globalThis.document=previous);
  let saved={definition:{id:"tools",name:"Tools",description:"Original",project_id:"p",enabled:true,expose_to_codex:false,session_scope:"consumer",allowed_tools:["echo"],transport:"stdio",command:["python","-B","service.py","--mode","stdio"],cwd:"/tools",url:"",environment:{PUBLIC_OPTION:"value"},environment_refs:{API_KEY:"env:TOOL_KEY"},header_refs:{},timeout_seconds:120},revision:1,connection_state:"disconnected",tool_count:null,active_calls:0,bound_agents:[]};
  const calls=[],state={project:{id:"p"},mcpServers:[],mcpQuery:"",mcpDirectory:"",mcpDraft:null,mcpEpoch:0};
  const rpc=async(operation,params)=>{calls.push({operation,params:structuredClone(params)});if(operation==="mcp.inspect")return structuredClone(saved);if(operation==="mcp.save"){assert.deepEqual(Object.keys(params).sort(),["definition","expected_revision"]);assert.equal(params.expected_revision,1);saved={...saved,definition:params.definition,revision:2};return structuredClone(saved);}throw new Error(operation);};
  const component=createMCPDirectory({state,escape:String,icon:()=>"",badge:String,markdown:String,rpc,toast:()=>{},render:()=>{},loadBootstrap:async()=>{},time:String});
  await component.action("mcp-select",{dataset:{id:"tools"}});
  component.input({closest:()=>null,dataset:{mcp:"description"},value:"Changed description",type:"text"});
  await assert.rejects(component.action("mcp-connect",{dataset:{id:"tools"}}),/先保存/);
  assert.equal(calls.some(value=>value.operation==="mcp.discover"),false);
  await component.submit({target:{id:"mcp-form"}});
  assert.deepEqual(saved.definition.command,["python","-B","service.py","--mode","stdio"]);
  assert.deepEqual(saved.definition.environment_refs,{API_KEY:"env:TOOL_KEY"});
  assert.deepEqual(saved.definition.environment,{PUBLIC_OPTION:"value"});
  assert.equal(saved.definition.session_scope,"consumer");
  assert.equal(saved.definition.expose_to_codex,false);
  assert.deepEqual(saved.definition.allowed_tools,["echo"]);
  assert.equal(saved.definition.description,"Changed description");
  assert.equal("max_concurrency" in saved.definition,false);
  assert.equal(state.mcpDraft.env_text,"");
});

test("a delayed Skill selection cannot overwrite another project's document view",async()=>{
  let release;const waiting=new Promise(resolve=>release=resolve);
  const state={project:{id:"p"},skillEpoch:0,skillRegistry:{items:[]}};
  const rpc=async operation=>{await waiting;if(operation==="skills.describe")return {definition:{id:"guide",name:"Guide",path:"/guide"},revision:1,bound_agents:[],metadata:{}};if(operation==="skills.read")return {text:"Original body",next_offset:null,sha256:"version"};if(operation==="skills.files")return {items:[],after:null};throw new Error(operation);};
  const component=createSkillIntegration({state,escape:String,icon:()=>"",markdown:String,rpc,toast:()=>{},render:()=>{},loadBootstrap:async()=>{}});
  const selecting=component.action("registry-skill-select",{dataset:{id:"guide"}});
  state.project={id:"q"};release();await selecting;
  assert.equal(state.skillDetail,undefined);
  assert.equal(state.skillSource,undefined);
});
