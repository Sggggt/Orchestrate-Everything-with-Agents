import test from "node:test";
import assert from "node:assert/strict";
import { applyModelChoice } from "../src/oea/web/static/conversation.mjs";
import { listChoices,modelOptions } from "../src/oea/web/static/api-box.mjs";

test("a new conversation selects a model without creating an instance until send",async()=>{
  const instance={id:"draft",project_id:"p",agent_id:"a",role:"main",draft:true};
  await applyModelChoice({},{instance,model:{id:"fast"}});
  assert.equal(instance.model_ref,"fast");
  await applyModelChoice({},{instance,model:null});
  assert.equal(instance.model_ref,null);
});

test("instance and Agent default mutations use independent revisions and refresh the proper scope",async()=>{
  const calls=[];
  const instance={id:"i",role:"main",project_id:"p",agent_id:"a",model_revision:8};
  const app={state:{selectedInstance:{...instance},instances:[{...instance}]},rpc:async(op,params)=>{
    calls.push([op,params]);
    if(op==="instances.set_model")return {...instance,model_ref:"m",model_revision:9,model_summary:{id:"m",name:"Fast"}};
  },reloadAgents:()=>calls.push("agents")};
  await applyModelChoice(app,{instance,model:{id:"m"}});
  assert.equal(app.state.selectedInstance.model_summary.name,"Fast");
  assert.equal(app.state.instances[0].model_revision,9);
  await applyModelChoice(app,{instance,agent:{revision:3},model:{id:"m"},agentDefault:true});
  assert.deepEqual(calls,[
    ["instances.set_model",{instance_id:"i",project_id:"p",model_ref:"m",expected_revision:8}],
    ["agents.set_model",{agent_id:"a",project_id:"p",model_ref:"m",expected_revision:3,role:"main"}],"agents",
  ]);
  for(const readonly of [{...instance,role:"manager"},{...instance,removed_at:1}]){
    await assert.rejects(applyModelChoice(app,{instance:readonly,model:{id:"m"}}),/只读/);
  }
  assert.equal(calls.length,3);
});

test("Box selection follows opaque page cursors and distinguishes aliases for the same upstream ID",async()=>{
  const cursors=[],model=id=>({id,name:id+"<",model_id:"upstream"});
  const app={rpc:async(_,params)=>{cursors.push(params.cursor);return params.cursor===0?{items:[model("a")],cursor:19}:{items:[model("b")],cursor:null};}};
  const models=await listChoices(app,"models.list");
  assert.deepEqual(cursors,[0,19]);
  assert.match(modelOptions(models,"b"),/value="b" selected>b&lt; · upstream/);
  assert.match(modelOptions(models,"removed"),/selected disabled value="removed"/);
});
