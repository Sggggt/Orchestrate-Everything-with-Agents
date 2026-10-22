// Execution observations come from the host; model output never sets UI state.
const labels = {
  CONNECTED: "已连接", CONNECTING: "连接中", DISCONNECTED: "未连接",
  DISABLED: "已停用", ERROR: "连接异常", DISPATCHED: "调用中",
  QUEUED: "排队中", RUNNING: "执行中", WAITING: "等待中",
  SUCCEEDED: "已完成", FAILED: "失败", CANCELLED: "已取消",
  SKIPPED: "已跳过", UNCERTAIN: "待核对", INCOMPLETE: "输出未完成",
  CANCELLING: "正在取消", FAILING: "正在停止", PENDING: "待应用",
  DELIVERED: "已入箱", OFFERED: "已交付", APPLIED: "已应用", REJECTED: "已拒绝", EXPIRED: "已过期",
  REPORTED: "问题报告", GUIDANCE: "请求指导", REPLIED: "已回复",
};

export const executionState = value => typeof value === "string" ? value : value?.execution_state || value?.state || "";
export const outcomeState = value => typeof value === "string" ? value : value?.state ||
  (value?.metadata?.acceptance?.state === "FAILED" ? "FAILED" : executionState(value));
export const statusLabel = value => {
  const state = outcomeState(value);
  if (state === "FAILED" && typeof value !== "string" && (value?.execution_state === "SUCCEEDED" ||
      value?.delivery_state === "FAILED" || value?.metadata?.acceptance?.state === "FAILED" ||
      value?.result?.metadata?.acceptance?.state === "FAILED")) return "交付未通过";
  return labels[state] || state;
};
export function matchesTaskFilter(task, filter) {
  const state = outcomeState(task);
  if (filter === "active") return ["RUNNING", "QUEUED"].includes(state);
  if (filter === "attention") return ["FAILED", "UNCERTAIN", "WAITING"].includes(state);
  if (filter === "done") return state === "SUCCEEDED";
  return true;
}
