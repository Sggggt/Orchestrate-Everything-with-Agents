import test from "node:test";
import assert from "node:assert/strict";
import { observeRun } from "../src/oea/web/static/run-stream.mjs";
import { MarkdownBlocks } from "../src/oea/web/static/stream-markdown.mjs";
import { markdown } from "../src/oea/web/static/markdown.mjs";
import { mergeEntries,conversationItems,conversationItemHTML } from "../src/oea/web/static/conversation.mjs";
import { overview,shell,agentCards } from "../src/oea/web/static/workbench-shell.mjs";

test("history reconciliation preserves older pages, updates receipts and never duplicates entries",()=>{
  const old=[{id:"a",position:1,text:"earlier"},{id:"b",position:2,text:"partial"}];
  assert.deepEqual(mergeEntries(old,[{id:"c",position:3,text:"latest"},{id:"b",position:2,text:"saved"}]).map(row=>row.text),["earlier","saved","latest"]);
});
test("tool-only turns collapse without hiding prose or crossing Run boundaries",()=>{
  const entries=[{id:"a",run_id:"r",role:"assistant",text:"",action_id:"m1"},{id:"b",run_id:"r",role:"assistant",text:null,action_id:"m2"},{id:"c",run_id:"r",role:"assistant",text:"**Ready** <script>"},{id:"d",run_id:"other",role:"assistant",text:null,action_id:"m3"}];
  const items=conversationItems(entries);
  assert.equal(items.length,3);assert.equal(items[0].entries.length,2);
  assert.match(conversationItemHTML(items[0]),/<details/);
  assert.match(conversationItemHTML(items[1]),/<strong>Ready<\/strong>/);
  assert.doesNotMatch(conversationItemHTML(items[1]),/<script>/);
  assert.doesNotMatch(conversationItemHTML(items[1]),/data-original-action/);
  assert.match(conversationItemHTML({kind:"assistant",entry:{...entries[2],action_id:"m4",characters:9000}}),/读取完整内容/);
});
test("run stream batches long output, deduplicates facts, retains interrupted text and closes cleanly",async t=>{
  t.mock.timers.enable({apis:["setTimeout"]});
  const listeners=new Set(),changes=[];let wire,syncs=0;
  const observer=observeRun({runId:"r",projectId:"p",listeners,source:url=>wire={url,close(){this.closed=true;}},onChange:value=>changes.push(value),onSync:()=>syncs++});
  const emit=frame=>wire.onmessage({data:JSON.stringify(frame)});
  wire.onopen();
  const started={position:1,run_id:"r",kind:"model.started",data:{action_id:"a",attempt_id:"u"}};
  for(const listener of listeners)listener(started);
  emit({kind:"event",event:started});
  for(let i=0;i<100;i++)emit({kind:"model.delta",action_id:"a",attempt_id:"u",attempt_position:1,sequence:i+1,text:"x".repeat(100)});
  assert.equal(changes.length,1);assert.equal(changes[0].text.length,100);t.mock.timers.tick(80);
  assert.equal(changes.length,2);assert.equal(changes.at(-1).text.length,10000);
  assert.equal(changes[0].actionId,"a");
  emit({kind:"event",event:{...started,position:2,kind:"model.completed"}});
  assert.equal(syncs,1);t.mock.timers.tick(80);
  assert.equal(changes.at(-1).text.length,10000,"keep text until the saved history arrives");
  wire.onerror();t.mock.timers.tick(80);
  assert.equal(changes.at(-1).connected,false);assert.equal(changes.at(-1).text.length,10000);
  wire.onopen();t.mock.timers.tick(80);assert.equal(changes.at(-1).text.length,10000);assert.equal(syncs,1);
  emit({kind:"model.delta",action_id:"a2",attempt_id:"u2",attempt_position:3,sequence:1,text:"partial"});
  emit({kind:"run.failed",run:{state:"failed"}});
  assert.equal(changes.at(-1).text,"partial");assert.equal(wire.closed,true);assert.equal(listeners.size,0);
  const count=changes.length;observer.close();t.mock.timers.tick(100);emit({kind:"model.delta",text:"late"});assert.equal(changes.length,count);
});
test("tool boundaries reconcile without joining distinct model turns",t=>{
  t.mock.timers.enable({apis:["setTimeout"]});let wire,latest;
  const observer=observeRun({runId:"r",projectId:"p",source:()=>wire={close(){}},onSync:()=>{},onChange:value=>latest=value});
  const emit=frame=>wire.onmessage({data:JSON.stringify(frame)});
  emit({kind:"model.delta",action_id:"first",attempt_id:"u1",attempt_position:1,sequence:1,text:"first"});emit({kind:"tool.observed",tool:"files.read",isError:false});t.mock.timers.tick(80);
  assert.equal(latest.text,"first");assert.match(latest.note,/files.read/);
  emit({kind:"event",event:{run_id:"r",position:2,kind:"model.started",data:{action_id:"second",attempt_id:"u2"}}});
  emit({kind:"model.delta",action_id:"second",attempt_id:"u2",attempt_position:2,sequence:1,text:"second"});
  emit({kind:"model.delta",action_id:"first",attempt_id:"u1",attempt_position:1,sequence:2,text:"old tail"});
  t.mock.timers.tick(80);assert.equal(latest.text,"second");observer.close();
});

test("hydrate buffers deltas, pins every page and discards duplicates",async t=>{
  t.mock.timers.enable({apis:["setTimeout"]});let wire,latest,resolve;const reads=[];
  const observer=observeRun({runId:"r",projectId:"p",source:()=>wire={close(){}},onSync:()=>{},onChange:value=>latest=value,
    readResponse:async params=>{reads.push(params);if(reads.length===1)return await new Promise(done=>resolve=done);return {...snapshot,text:"B",next_offset:null};}});
  const emit=frame=>wire.onmessage({data:JSON.stringify(frame)});
  const snapshot={available:true,action_id:"a",attempt_id:"u",attempt_position:1,sequence:2,version:"live:u:2",status:"streaming",text:"A",next_offset:1};
  wire.onopen();
  emit({kind:"model.delta",action_id:"a",attempt_id:"u",attempt_position:1,sequence:2,text:"B"});
  emit({kind:"model.delta",action_id:"a",attempt_id:"u",attempt_position:1,sequence:3,text:"C"});
  resolve(snapshot);await new Promise(setImmediate);t.mock.timers.tick(80);
  assert.equal(latest.text,"ABC");assert.equal(reads[1].version,"live:u:2");assert.equal(reads[1].attempt_id,"u");observer.close();
});

test("a gap reads exactly that attempt and an obsolete connection cannot overwrite it",async t=>{
  t.mock.timers.enable({apis:["setTimeout"]});let wire,latest;const reads=[],pending=[];
  const observer=observeRun({runId:"r",projectId:"p",source:()=>wire={close(){}},onSync:()=>{},onChange:value=>latest=value,
    readResponse:params=>{reads.push(params);return new Promise(resolve=>pending.push(resolve));}});
  const emit=(sequence,text)=>wire.onmessage({data:JSON.stringify({kind:"model.delta",action_id:"a",attempt_id:"u",attempt_position:1,sequence,text})});
  emit(1,"A");emit(3,"C");assert.equal(reads[0].action_id,"a");assert.equal(reads[0].attempt_id,"u");
  wire.onerror();wire.onopen();
  pending[1]({available:true,action_id:"a",attempt_id:"u",attempt_position:1,sequence:3,status:"streaming",text:"ABC",next_offset:null});
  await new Promise(setImmediate);
  pending[0]({available:true,action_id:"a",attempt_id:"u",attempt_position:1,sequence:1,status:"streaming",text:"obsolete",next_offset:null});
  await new Promise(setImmediate);t.mock.timers.tick(80);assert.equal(latest.text,"ABC");observer.close();
});

test("stable Markdown blocks match full rendering across every token boundary",()=>{
  const text="# Title\n\nParagraph **bold**.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n```js\nconst a = 1;\n\n// blank inside fence\n```\n\n- one\n- two\n\n> quote\n> next\n\nTail <script>\n";
  for(const width of [1,2,7,79,text.length]){
    const projection=new MarkdownBlocks(),blocks=[];let value;
    for(let end=width;end<text.length+width;end+=width){value=projection.update(text.slice(0,end));blocks.push(...value.blocks.map(item=>item.html));}
    assert.equal([...blocks,markdown(value.tail)].filter(Boolean).join("\n"),markdown(text));
    assert.equal(projection.update("replacement").reset,true);
  }
});
test("overview keeps task entry points without the redundant hero",()=>{
  const state={agents:[],instances:[],runs:[{id:"r",agent_id:"worker",state:"waiting",goal:"# Review",instance_name:"Worker",created_at:0}],artifacts:[],summary:{},projects:[],sessions:[],openTabs:[],page:"overview"};
  const html=overview(state);
  assert.doesNotMatch(html,/workspace-brief|next-step|查看任务/);
  assert.match(html,/建立工作流/);
  assert.equal((html.match(/data-run="r"/g)||[]).length,2);
  assert.equal((html.match(/data-page="workflows"/g)||[]).length,1);
  assert.equal((shell(state).match(/data-action="search"/g)||[]).length,1);
});


test("Agent entries always offer a conversation and a separate settings gear",()=>{
  const html=agentCards({agents:[{definition:{id:"worker",name:"Worker",enabled:false,description:"",model:{},execution:{program:[]},instructions:{system:""}}}],instances:[]});
  assert.match(html,/data-agent-chat="worker"/);assert.match(html,/data-configure-agent="worker"/);
  assert.doesNotMatch(html,/完成配置/);
});
