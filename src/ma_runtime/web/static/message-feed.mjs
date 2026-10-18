// Use the session's bounded presentation; complete peer payloads stay on the host.
export function messageItems(snapshot) {
  const messages = (snapshot.messages || []).map(message => ({
    ...message, kind: "message",
    summary_markdown: message.envelope?.payload?.summary_markdown || "协作消息已保存，完整内容已传递给接收 Agent。",
  }));
  const reports = (snapshot.agent_reports || []).map(report => ({
    ...report, kind: "report", source_thread_id: report.thread_id,
    state: report.replied ? "REPLIED" : report.wait_for_guidance ? "GUIDANCE" : "REPORTED",
  }));
  return [...messages, ...reports].sort((a, b) => b.created_at - a.created_at);
}

export function matchesMessageFilter(item, filter) {
  return filter === "all" || (filter === "reports" ? item.kind === "report" : item.kind === "message" && item.state === filter);
}

export function agentFeedback(items, limit = 3) {
  return items
    .filter(item => item.kind === "report" || (item.source_thread_id && item.envelope?.source_run_id))
    .slice(0, limit);
}
