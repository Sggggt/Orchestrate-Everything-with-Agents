import test from "node:test";
import assert from "node:assert/strict";
import { createMCPDirectory } from "../src/oea/web/static/mcp-directory.mjs";

test("MCP history follows the server cursor and keeps prior rows",async()=>{
  const state={project:{id:"p"},mcpServers:[],mcpQuery:"",mcpId:"business",mcpDraft:{},mcpDetail:{},mcpTab:"calls",mcpCalls:[],mcpEpoch:0};
  const requests=[];
  const component=createMCPDirectory({state,escape:String,icon:()=>"",badge:String,markdown:String,toast:()=>{},render:()=>{},loadBootstrap:()=>{},showCredential:()=>{},showSDK:()=>{},showImport:()=>{},showRemove:()=>{},showToolTrial:()=>{},time:String,rpc:async(name,params)=>{requests.push({name,params});return {items:[{id:params.cursor?"older":"newer",state:"completed",created_at:1,updated_at:2}],cursor:params.cursor?null:4};}});
  await component.action("mcp-refresh-calls",{dataset:{}});
  assert.match(component.page(),/mcp-more-calls/);
  await component.action("mcp-more-calls",{dataset:{}});
  assert.deepEqual(state.mcpCalls.map(row=>row.id),["newer","older"]);
  assert.equal(requests[1].params.cursor,4);
  assert.doesNotMatch(component.page(),/mcp-more-calls/);
});
