import test from "node:test";
import assert from "node:assert/strict";
import {usageRows,projectionRows,cancellationHTML,diagnosisHTML,integrityLabel} from "../src/oea/web/static/diagnostics.mjs";

test("usage separates provider reports from accounting and unknown values",()=>{
  const html=usageRows({usage:{model_calls:2,input_tokens:300,output_tokens:400},provider_reported:{input_tokens:10,output_tokens:null},estimated:{input_tokens:100,output_tokens:200},reserved:{input_tokens:0,output_tokens:0},needs_review:{input_tokens:200,output_tokens:200}});
  assert.match(html,/供应商报告 输入 \/ 输出<\/dt><dd>10 \/ 未知/);
  assert.match(html,/预算核算 输入 \/ 输出<\/dt><dd>300 \/ 400/);
  assert.match(html,/待核对/);
  assert.match(html,/供应商缓存命中<\/dt><dd>未知/);
});
test("input prediction and physical cancellation remain distinct from delivery",()=>{
  const html=projectionRows({projection:{input_tokens:120,input_tokens_before:240,budget_tokens:1000,components:{tool_schemas:40},tools:{loaded:1,deferred:3}},comparison:{provider_input_tokens:130,difference_tokens:10}});
  assert.match(html,/240 → 120/);assert.match(html,/130 \/ 10/);
  assert.match(cancellationHTML({cancel_requested_at:1,in_flight:true,stop_confirmed:false}),/仍在进行/);
  assert.doesNotMatch(cancellationHTML({cancel_requested_at:1,in_flight:true,stop_confirmed:false}),/已确认/);
  assert.match(cancellationHTML({cancel_requested_at:1,in_flight:false,stop_confirmed:true}),/已确认执行停止/);
});
test("diagnosis renders the shared root cause and original receipt safely",()=>{
  const html=diagnosisHTML({diagnostic:{what:"Summary failed",cause:"<script>provider error</script>",origin:"provider",send_started:true,response_received:true,integrity:"partial",evidence_ref:{action_id:"a",attempt_id:"try"}}});
  assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
  assert.match(html,/data-evidence-attempt="try"/);assert.match(html,/已进入发送/);
  assert.equal(integrityLabel("incomplete"),"生成未完整结束");
});
