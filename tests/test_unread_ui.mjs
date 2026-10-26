import test from "node:test";
import assert from "node:assert/strict";
import {unreadBadge,agentCards,shell} from "../src/oea/web/static/workbench-shell.mjs";
import {messageSource} from "../src/oea/web/static/chat.mjs";

const agent={id:"worker",name:"Worker",enabled:true,model:{model:"fixture",base_url:"https://example.invalid"},execution:{program:[]},instructions:{system:""},description:""};
const state={page:"overview",agents:[{definition:agent}],instances:[{id:"i",agent_id:"worker",role:"main",name:"One"}],projects:[],sessions:[],openTabs:[],runs:[],artifacts:[],summary:{},unread:{messages:{count:3},agents:{worker:{instances:101,count:120}}}};

test("unread badges are separate from Agent state and use full aggregate counts",()=>{
 const cards=agentCards(state),frame=shell(state);
 assert.match(cards,/<h3 class="agent-card-name"><span>Worker<\/span><span class="badge/);
 assert.match(cards,/120 条未读消息/);
 assert.match(frame,/101 个实例/);
 assert.match(frame,/3 条未读消息/);
 assert.match(unreadBadge(120),/>99\+</);
 assert.equal(unreadBadge(0),"");
});
test("message labels use the accepted external name or exact instance identity",()=>{
 assert.equal(messageSource({source:{kind:"leader",name:"codex"}},state).name,"codex");
 assert.equal(messageSource({source:{kind:"leader"}},state).name,"leader");
 assert.equal(messageSource({source:{kind:"instance",instance_id:"i"}},state).instanceId,"i");
 assert.equal(messageSource({source:{kind:"manager",instance_id:"i",agent_id:"worker"}},state).agentId,"worker");
});
