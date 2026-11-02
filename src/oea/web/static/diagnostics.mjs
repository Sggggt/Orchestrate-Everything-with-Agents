import {escape,icon} from "./ui.mjs";

const number=value=>value==null?"未知":Number(value).toLocaleString();
const tokens=value=>`${number(value?.input_tokens)} / ${number(value?.output_tokens)}`;
export function usageRows(budget){
  const attempts=budget.attempts,provider=budget.provider_reported,usage=budget.usage;
  const scopes={run_direct:"本次运行自身",root_work:"根工作及子工作",shared_budget:"共享预算总账"};
  const reserves=budget.reserves;
  const reserveRows=reserves?`<dt>预算阶段</dt><dd>${budget.phase==="finalizing"?"收尾":"执行"}</dd><dt>原预算内剩余预留</dt><dd>收尾 ${number(reserves.finalization.output_tokens)} / 恢复 ${number(reserves.recovery.output_tokens)} token${reserves.finalization.cost_micros==null?"":` · ${escape(reserves.currency)} 收尾 ${number(reserves.finalization.cost_micros/1e6)} / 恢复 ${number(reserves.recovery.cost_micros/1e6)}`}</dd><dt>剩余费用额度</dt><dd>${budget.policy?.max_cost==null?"未设费用上限":`${escape(reserves.currency)} ${budget.remaining?.cost_micros==null?"未知":number(budget.remaining.cost_micros/1e6)}`}</dd>`:"";
  const amounts=Object.entries(budget.cost?.currencies||{}).map(([currency,cost])=>`${escape(currency)} · 已结算 ${(cost.settled_micros/1e6).toLocaleString(undefined,{maximumFractionDigits:6})} · 估算 ${(cost.estimated_micros/1e6).toLocaleString(undefined,{maximumFractionDigits:6})} · 在途 ${(cost.reserved_micros/1e6).toLocaleString(undefined,{maximumFractionDigits:6})} · 待核对 ${(cost.pending_micros/1e6).toLocaleString(undefined,{maximumFractionDigits:6})} · 价格修订 ${escape(cost.price_revisions.join(", "))}`);
  return `<dt>统计范围</dt><dd>${scopes[budget.scope]||escape(budget.scope||"共享预算总账")}${budget.as_of?" · "+new Date(budget.as_of).toLocaleTimeString():""}</dd><dt>调用尝试</dt><dd>${number(usage.model_calls)}</dd><dt>预算核算 总输入 / 输出</dt><dd>${tokens(usage)}</dd><dt>供应商报告 总输入 / 输出</dt><dd>${tokens(provider)}</dd><dt>非缓存 / 缓存读 / 缓存写</dt><dd>${number(usage.input_uncached)} / ${number(usage.cache_read)} / ${number(usage.cache_write)} token</dd><dt>推理 token（包含在输出内）</dt><dd>${number(usage.reasoning)}</dd><dt>费用</dt><dd>${amounts.join("<br>")||"未知，未配置价格"}${budget.cost?.unknown_calls?" · "+number(budget.cost.unknown_calls)+" 次费用未知":""}</dd>${reserveRows}<dt>估算 输入 / 输出</dt><dd>${tokens(budget.estimated)}</dd><dt>在途预留 输入 / 输出</dt><dd>${tokens(budget.reserved)}</dd><dt>待核对 输入 / 输出</dt><dd>${tokens(budget.needs_review)}</dd>${attempts?`<dt>尝试事实</dt><dd>发送前拒绝 ${attempts.rejected_before_send} · 收到响应 ${attempts.responses_received} · 生成完成 ${attempts.generation_completed} · 失败 ${attempts.failed}${attempts.historical_unknown?` · ${attempts.historical_unknown} 次历史事实未知`:""}</dd>`:""}`;
}
export function projectionRows(input){
  const value=input?.projection;if(!value)return "";
  const comparison=input.comparison;
  return `<dt>当前工作输入估算</dt><dd>${number(value.input_tokens)} / ${number(value.budget_tokens)} token</dd><dt>工具 Schema 估算</dt><dd>${number(value.components?.tool_schemas)} token · 已加载 ${number(value.tools?.loaded)} / ${number(value.tools?.deferred)}</dd><dt>投影缩减</dt><dd>${number(value.input_tokens_before)} → ${number(value.input_tokens)} token</dd>${comparison?`<dt>本次供应商输入 / 预测偏差</dt><dd>${number(comparison.provider_input_tokens)} / ${number(comparison.difference_tokens)} token</dd>`:""}`;
}
export function cancellationHTML(run){
  if(!run.cancel_requested_at)return "";
  return `<p class="run-wait">${run.stop_confirmed?"已确认执行停止":run.in_flight?"已请求取消 · 执行仍在进行，等待停止确认":"已请求取消 · 尚未确认执行停止"}</p>`;
}
export function diagnosisHTML(error){
  if(!error)return "";
  const value=error.diagnostic;
  if(!value)return `<p class="v2-error">${escape(error.safe_message)}</p>`;
  const origin=({local:"本地",transport:"链路",provider:"供应商",unknown:"来源未知"})[value.origin]||value.origin;
  const sent=value.send_started==null?"发送状态未知":value.send_started?"已进入发送":"发送前停止";
  const integrity=({complete:"已收到内容完整",partial:"仅保留部分内容",truncated:"原始内容受捕获上限影响",incomplete:"生成未完整结束",unknown:"完整性未知"})[value.integrity]||value.integrity;
  const reference=value.evidence_ref;
  const fields=(value.field_errors||[]).map(item=>`<li>${escape(item.path.join("."))}: ${escape(item.message)}</li>`).join("");
  return `<div class="run-diagnostic"><p>${escape(value.what)}</p>${value.cause!==value.what?`<p>${escape(value.cause)}</p>`:""}${fields?`<ul>${fields}</ul>`:""}<small>${escape(origin)} · ${sent}${value.response_received===true?" · 已收到响应":""} · ${escape(integrity)}</small>${value.continuation==="inspect_then_resume"?"<p>核对原回执和原因后，可显式继续原工作。</p>":""}${reference?.action_id?`<button class="icon-btn" data-evidence-action="${escape(reference.action_id)}" data-evidence-attempt="${escape(reference.attempt_id||"")}" title="读取原回执" aria-label="读取原回执">${icon("file")}</button>`:""}</div>`;
}
export function integrityLabel(value){return ({incomplete:"生成未完整结束",partial:"部分内容已保留",truncated:"原始内容已截断"})[value]||"";}
