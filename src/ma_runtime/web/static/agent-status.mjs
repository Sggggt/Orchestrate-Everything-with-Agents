// Project current session state without another subscription or maintenance request.
const statuses = {
  UNCERTAIN: { tone: "error", label: "待核对", priority: 6 },
  FAILED: { tone: "error", label: "执行失败", priority: 5 },
  GUIDANCE: { tone: "waiting", label: "待回复报告", priority: 4 },
  WAITING: { tone: "waiting", label: "等待中", priority: 3 },
  RUNNING: { tone: "running", label: "执行中", priority: 2 },
  QUEUED: { tone: "muted", label: "排队中", priority: 1 },
  READY: { tone: "ready", label: "准备就绪", priority: 0 },
  DISABLED: { tone: "muted", label: "已停用", priority: 0 },
  UNCONFIGURED: { tone: "muted", label: "待配置模型", priority: 0 },
  OFFLINE: { tone: "muted", label: "连接中断，状态待确认", priority: 0 },
  CONNECTING: { tone: "muted", label: "连接中，状态待确认", priority: 0 },
};
const active = new Set(["RUNNING", "WAITING", "QUEUED", "UNCERTAIN"]);
const choose = (a, b) => !a || b.priority > a.priority ? b : a;

export function createAgentStatusIndex(agents, snapshot, connection = "online") {
  const configs = new Map(agents.map(a => [a.id, a]));
  const latest = new Map(), threads = new Map(), byAgent = new Map();
  const tasks = snapshot.tasks || [];
  const assign = (threadId, status) => threads.set(threadId, choose(threads.get(threadId), status));
  for (const task of tasks) {
    const previous = latest.get(task.thread_id);
    if (!previous || task.created_at > previous.created_at) latest.set(task.thread_id, task);
    if (active.has(task.state)) assign(task.thread_id, statuses[task.state]);
  }
  for (const task of latest.values()) {
    if (task.state === "FAILED") assign(task.thread_id, statuses.FAILED);
  }
  for (const threadId of snapshot.failed_thread_ids || []) assign(threadId, statuses.FAILED);
  for (const threadId of snapshot.attention_thread_ids || []) assign(threadId, statuses.GUIDANCE);
  for (const thread of snapshot.threads || []) {
    const status = threads.get(thread.id);
    if (status) byAgent.set(thread.agent_id, choose(byAgent.get(thread.agent_id), status));
  }
  function display(agentId, current) {
    if (connection !== "online") return connection === "connecting" ? statuses.CONNECTING : statuses.OFFLINE;
    if (current) return current;
    const a = configs.get(agentId);
    if (!a?.enabled) return statuses.DISABLED;
    if ((a.kind === "llm" || (a.kind === "process" && a.llm_required)) && !(a.base_url?.trim() && a.model?.trim())) return statuses.UNCONFIGURED;
    return statuses.READY;
  }
  return {
    forAgent: id => display(id, byAgent.get(id)),
    forThread: thread => display(thread.agent_id, threads.get(thread.id)),
  };
}
