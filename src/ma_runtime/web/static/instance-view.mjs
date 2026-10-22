// Durable activity determines ordering; token streams never enter this model.
export function instanceInUse(instance) {
  return !!(instance.active_run_id || instance.owner_task_id ||
    ["RUNNING", "QUEUED", "WAITING", "UNCERTAIN"].includes(instance.execution_state || instance.state));
}

export function instanceGroups(agents, snapshot) {
  const details = new Map((snapshot.thread_activity || []).map(item => [item.thread_id, item]));
  return agents.filter(agent => agent.kind !== "external").map(agent => {
    const instances = (snapshot.threads || []).filter(thread => thread.agent_id === agent.id).map(thread => ({
      ...thread, thread_id: thread.id, activity_position: 0, unread_count: 0, execution_state: "READY",
      ...details.get(thread.id),
    })).sort(compareInstances);
    const counts = { RUNNING: 0, WAITING: 0, FAILED: 0, UNCERTAIN: 0, QUEUED: 0 };
    for (const item of instances) if (item.execution_state in counts) counts[item.execution_state]++;
    return { agent, instances, counts,
      activity_position: Math.max(0, ...instances.map(item => item.activity_position)),
      unread_count: instances.reduce((sum, item) => sum + item.unread_count, 0),
    };
  }).sort((a, b) => b.activity_position - a.activity_position || a.agent.id.localeCompare(b.agent.id));
}

export function compareInstances(a, b) {
  return b.activity_position - a.activity_position || b.created_at - a.created_at || b.thread_id.localeCompare(a.thread_id);
}

export function conversationItems(conversation, activity) {
  const positions = new Map((activity?.items || []).filter(item => item.kind === "conversation").map(item => [item.source_id, item]));
  const entries = (conversation?.items || []).map(entry => ({ ...entry, activity: positions.get(entry.id) }));
  const notices = (activity?.items || []).filter(item => item.kind !== "conversation").map(item => ({
    id: item.source_id, created_at: item.created_at, notice: true, activity: item,
  }));
  return [...entries, ...notices].sort((a, b) => a.created_at - b.created_at || String(a.id).localeCompare(String(b.id)));
}

// Keep explicitly loaded history across live refreshes; ordinary tails stay bounded.
export function mergeHistoryPage(current, page, older = false, key = "id") {
  if (!current || (!older && !current.history_loaded)) return page;
  const items = [...new Map([...current.items, ...page.items].map(item => [item[key], item])).values()]
    .sort((a, b) => (a.seq ?? a.position) - (b.seq ?? b.position));
  return { ...page, items, history_loaded: true,
    ...(!older ? { cursor: current.cursor, has_more: current.has_more } : {}) };
}

export function orderedRuns(runs) {
  const priority = state => ["RUNNING", "CANCELLING", "FAILING"].includes(state) ? 0 : ["WAITING", "UNCERTAIN"].includes(state) ? 1 : 2;
  return [...runs].sort((a, b) => priority(a.state) - priority(b.state) ||
    b.created_at - a.created_at || b.id.localeCompare(a.id));
}
