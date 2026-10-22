// Availability comes from current configuration and ownership, not past messages.
import { executionState } from "./execution-status.mjs";

const statuses = {
  BLOCKED: { tone: "waiting", label: "暂不可继续", hint: "先核对当前实例的执行结果，再继续对话", priority: 4 },
  WAITING: { tone: "waiting", label: "等待中", priority: 3 },
  RUNNING: { tone: "running", label: "工作中", hint: "正在处理任务，仍可发送后续指令", priority: 2 },
  QUEUED: { tone: "muted", label: "排队中", priority: 1 },
  READY: { tone: "ready", label: "可对话", hint: "执行端已配置，可发送指令", priority: 0 },
  DISABLED: { tone: "muted", label: "已停用", priority: 0 },
  UNCONFIGURED: { tone: "muted", label: "待配置模型", priority: 0 },
  OFFLINE: { tone: "muted", label: "连接中断，状态待确认", priority: 0 },
  CONNECTING: { tone: "muted", label: "连接中，状态待确认", priority: 0 },
};
const active = new Set(["RUNNING", "WAITING", "QUEUED"]);
const choose = (a, b) => !a || b.priority > a.priority ? b : a;

export function createAgentStatusIndex(agents, snapshot, connection = "online") {
  const configs = new Map(agents.map(a => [a.id, a]));
  const threads = new Map(), byAgent = new Map();
  const tasks = snapshot.tasks || [];
  const assign = (threadId, status) => threads.set(threadId, choose(threads.get(threadId), status));
  for (const task of tasks) {
    const observed = executionState(task);
    if (active.has(observed)) assign(task.thread_id, statuses[observed]);
    if (observed === "UNCERTAIN") assign(task.thread_id, statuses.BLOCKED);
  }
  for (const thread of snapshot.threads || []) {
    const status = threads.get(thread.id);
    if (status && status !== statuses.BLOCKED) byAgent.set(thread.agent_id, choose(byAgent.get(thread.agent_id), status));
  }
  function display(agentId, current) {
    if (connection !== "online") return connection === "connecting" ? statuses.CONNECTING : statuses.OFFLINE;
    const a = configs.get(agentId);
    if (!a?.enabled) return statuses.DISABLED;
    if ((a.kind === "llm" || (a.kind === "process" && a.llm_required)) && !(a.base_url?.trim() && a.model?.trim())) return statuses.UNCONFIGURED;
    return current || statuses.READY;
  }
  return {
    forAgent: id => display(id, byAgent.get(id)),
    forThread: thread => {
      const observed = executionState(thread);
      return display(thread.agent_id, thread.blocked_reason || observed === "UNCERTAIN" ? statuses.BLOCKED :
        threads.get(thread.thread_id || thread.id) || (active.has(observed) ? statuses[observed] : null));
    },
  };
}
