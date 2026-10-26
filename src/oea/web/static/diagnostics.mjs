import {escape,icon} from "./ui.mjs";

const number=value=>value==null?"未知":Number(value).toLocaleString();
const tokens=value=>`${number(value?.input_tokens)} / ${number(value?.output_tokens)}`;
export function usageRows(budget){
  const attempts=budget.attempts,provider=budget.provider_reported;
  return `<dt>调用尝试</dt><dd>${number(budget.usage.model_calls)}</dd><dt>预算核算 输入 / 输出</dt><dd>${tokens(budget.usage)}</dd><dt>供应商报告 输入 / 输出</dt><dd>${tokens(provider)}</dd><dt>估算 输入 / 输出</dt><dd>${tokens(budget.estimated)}</dd><dt>在途预留 输入 / 输出</dt><dd>${tokens(budget.reserved)}</dd><dt>待核对 输入 / 输出</dt><dd>${tokens(budget.needs_review)}</dd>${attempts?`<dt>尝试事实</dt><dd>发送前拒绝 ${attempts.rejected_before_send} · 收到响应 ${attempts.responses_received} · 生成完成 ${attempts.generation_completed} · 失败 ${attempts.failed}${attempts.historical_unknown?` · ${attempts.historical_unknown} 次历史事实未知`:""}</dd>`:""}`;
}
export function diagnosisHTML(error){
  if(!error)return "";
  const value=error.diagnostic;
  if(!value)return `<p class="v2-error">${escape(error.safe_message)}</p>`;
  const origin=({local:"本地",transport:"链路",provider:"供应商",unknown:"来源未知"})[value.origin]||value.origin;
  const sent=value.send_started==null?"发送状态未知":value.send_started?"已进入发送":"发送前停止";
  const integrity=({complete:"已收到内容完整",partial:"仅保留部分内容",truncated:"原始内容受捕获上限影响",incomplete:"生成未完整结束",unknown:"完整性未知"})[value.integrity]||value.integrity;
  const reference=value.evidence_ref;
  return `<div class="run-diagnostic"><p>${escape(value.what)}</p>${value.cause!==value.what?`<p>${escape(value.cause)}</p>`:""}<small>${escape(origin)} · ${sent}${value.response_received===true?" · 已收到响应":""} · ${escape(integrity)}</small>${value.continuation==="inspect_then_resume"?"<p>核对原回执和原因后，可显式继续原工作。</p>":""}${reference?.action_id?`<button class="icon-btn" data-evidence-action="${escape(reference.action_id)}" data-evidence-attempt="${escape(reference.attempt_id||"")}" title="读取原回执" aria-label="读取原回执">${icon("file")}</button>`:""}</div>`;
}
export function integrityLabel(value){return ({incomplete:"生成未完整结束",partial:"部分内容已保留",truncated:"原始内容已截断"})[value]||"";}
