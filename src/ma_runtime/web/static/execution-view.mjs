const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const states = { RUNNING: "执行中", SUCCEEDED: "已完成", FAILED: "失败", CANCELLED: "已取消", INCOMPLETE: "输出未完成", UNCERTAIN: "待核对", WAITING: "等待中", DISPATCHED: "调用中" };
const row = (label, value) => `<div class="kv"><span>${escape(label)}</span><strong>${escape(value)}</strong></div>`;
const failure = error => error ? `<p class="execution-error">${escape(error.safe_message || error.code)}</p>` : "";

export function duration(value) {
  return typeof value !== "number" || !Number.isFinite(value) ? "—" : value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}

export function canContinue(task, thread, now = Date.now()) {
  const last = task?.runs?.at(-1)?.boundary_receipt?.checkpoint_id;
  const budget = task?.continuation;
  return !!(task?.state === "FAILED" && task.result && !task.spec?.metadata?.workflow_run_id && last &&
    last === thread?.latest_checkpoint_id && !thread.active_run_id && !thread.owner_task_id &&
    thread.adapter_snapshot?.supports_suspend &&
    ["OUTPUT_TRUNCATED", "EMPTY_OUTPUT", "OUTPUT_SCHEMA_MISMATCH", "COVERAGE_REQUIREMENT_UNMET", "ARTIFACT_REQUIREMENT_UNMET"].includes(task.error?.code) &&
    (budget ? budget.runs_used < budget.max_runs && budget.calls_used < budget.max_model_calls &&
      budget.tokens_reserved < budget.max_output_tokens && now < budget.deadline_at : (task.spec?.continuation_policy?.max_runs ?? 2) > 0));
}

export function executionTrace(trace, receipts = {}, loading = new Set()) {
  const requests = new Map();
  for (const item of trace.items || []) {
    const previous = requests.get(item.id);
    if (!previous || (item.seq ?? 0) >= (previous.seq ?? 0)) requests.set(item.id, item);
  }
  const tools = trace.tools || [];
  const tool = call => {
    const receipt = receipts[call.id];
    const source = receipt ? JSON.stringify(receipt, null, 2) : "";
    return `<div class="execution-tool">${row(call.tool || call.tool_name, `${states[call.state] || call.state} · ${duration(call.duration_ms)}`)}${failure(call.error)}<button class="text-link" data-action="tool-receipt" data-id="${escape(call.id)}" ${loading.has(call.id) ? "disabled" : ""}>${loading.has(call.id) ? "正在读取…" : receipt ? "收起调用回执" : "查看调用回执"}</button>${receipt ? `<pre class="execution-receipt">${escape(source.slice(0, 64000))}</pre>${source.length > 64000 ? '<p class="form-hint">此处展示前 64,000 字符，完整回执可通过 SDK 读取。</p>' : ""}` : ""}</div>`;
  };
  let number = 0;
  const stage = item => {
    const evidence = item.stage === "context_read";
    const label = evidence ? "读取证据" : item.stage === "summary" ? "整理上下文" : `模型请求 ${++number}`;
    const attrs = item.attributes || {};
    const children = [...requests.values()].filter(child => child.attributes?.parent_stage_id === item.id);
    return `<details class="execution-request" data-trace-node="${escape(item.id)}"><summary>${escape(label)}<span>${escape(states[item.state] || item.state)} · ${duration(item.duration_ms)}</span></summary>${failure(item.error)}${attrs.response_headers_ms != null ? row("收到响应头", duration(attrs.response_headers_ms)) : ""}${attrs.first_content_ms != null ? row("首段正文", duration(attrs.first_content_ms)) : ""}${attrs.context ? row("输入估算 / 预算", `${attrs.context.input_tokens} / ${attrs.context.budget_tokens} tokens`) : ""}${attrs.usage?.total_tokens != null ? row("Token 用量", attrs.usage.total_tokens) : ""}${evidence ? row("已读字节", attrs.bytes_read ?? "—") : ""}${tools.filter(call => call.parent_stage_id === item.id).map(tool).join("")}${children.map(stage).join("")}</details>`;
  };
  const stages = [...requests.values()];
  const forRun = id => stages.filter(item => item.run_id === id && !requests.has(item.attributes?.parent_stage_id)).map(stage).join("") +
    tools.filter(call => call.run_id === id && !requests.has(call.parent_stage_id)).map(tool).join("");
  const runs = trace.runs || [];
  return `<div class="execution-trace">${row("排队", duration(trace.queued_ms))}${runs.map(run => `<section class="execution-run">${row(`第 ${run.attempt} 次执行`, `${states[run.state] || run.state} · ${duration(run.duration_ms)}`)}${failure(run.error)}${run.timings?.preparation_ms != null ? row("准备", duration(run.timings.preparation_ms)) : ""}${forRun(run.id)}${run.timings?.validation_ms != null ? row("交付检查", duration(run.timings.validation_ms)) : ""}${run.timings?.commit_work_ms != null ? row("入库处理", duration(run.timings.commit_work_ms)) : ""}</section>`).join("")}${forRun(undefined)}${trace.tools_has_more ? '<button class="text-link" data-action="trace-tools">加载更多工具记录</button>' : ""}</div>`;
}
