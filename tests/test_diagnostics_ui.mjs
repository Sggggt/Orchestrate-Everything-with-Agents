import test from "node:test";
import assert from "node:assert/strict";
import {usageRows,diagnosisHTML,integrityLabel} from "../src/oea/web/static/diagnostics.mjs";

test("usage separates provider reports from accounting and unknown values",()=>{
  const html=usageRows({usage:{model_calls:2,input_tokens:300,output_tokens:400},provider_reported:{input_tokens:10,output_tokens:null},estimated:{input_tokens:100,output_tokens:200},reserved:{input_tokens:0,output_tokens:0},needs_review:{input_tokens:200,output_tokens:200}});
  assert.match(html,/供应商报告 输入 \/ 输出<\/dt><dd>10 \/ 未知/);
  assert.match(html,/预算核算 输入 \/ 输出<\/dt><dd>300 \/ 400/);
  assert.match(html,/待核对/);
});
test("diagnosis renders the shared root cause and original receipt safely",()=>{
  const html=diagnosisHTML({diagnostic:{what:"Summary failed",cause:"<script>provider error</script>",origin:"provider",send_started:true,response_received:true,integrity:"partial",evidence_ref:{action_id:"a",attempt_id:"try"}}});
  assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
  assert.match(html,/data-evidence-attempt="try"/);assert.match(html,/已进入发送/);
  assert.equal(integrityLabel("incomplete"),"生成未完整结束");
});
