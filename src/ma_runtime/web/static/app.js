import { createIntegrationUI } from "/assets/integrations.js";
import { markdown } from "/assets/markdown.mjs";
import { content, compactTitle, editorInteraction } from "/assets/presentation.mjs";
import { createLaunchEditor } from "/assets/launch.mjs";
import { artifactDownload, artifactSource, groupArtifacts, readArtifactPreview } from "/assets/artifacts.mjs";
import { createWorkflowUI } from "/assets/workflows.js";
import { createAgentStatusIndex } from "/assets/agent-status.mjs";
import { messageItems, matchesMessageFilter } from "/assets/message-feed.mjs";
import { instanceGroups, instanceInUse, conversationItems, mergeHistoryPage } from "/assets/instance-view.mjs";
import { api } from "/assets/api.mjs";
import { canContinue, executionTrace } from "/assets/execution-view.mjs";
import { executionState, outcomeState, statusLabel, matchesTaskFilter } from "/assets/execution-status.mjs";

const $ = (selector, root = document) => root.querySelector(selector);
const escape = (value = "") =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const paths = {
  plug: "M8 2v5 M16 2v5 M6 7h12v4a6 6 0 0 1-6 6v5 M12 17a6 6 0 0 1-6-6",
  book: "M4 3h7a3 3 0 0 1 3 3v15a4 4 0 0 0-4-2H4z M14 6a3 3 0 0 1 3-3h4v16h-3a4 4 0 0 0-4 2",
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  agents:
    "M8 8a4 4 0 1 0 8 0a4 4 0 0 0-8 0 M4 21v-2a8 8 0 0 1 16 0v2 M3 8H1 M23 8h-2",
  settings:
    "M10 2h4l.5 3 2 1.2 2.8-1 2 3.4-2.3 2v2.8l2.3 2-2 3.4-2.8-1-2 1.2-.5 3h-4l-.5-3-2-1.2-2.8 1-2-3.4 2.3-2v-2.8l-2.3-2 2-3.4 2.8 1 2-1.2Z M12 8a4 4 0 1 0 0 8a4 4 0 0 0 0-8",
  chat: "M21 11a8 8 0 0 1-8 8H6l-4 3V11a8 8 0 0 1 8-8h3a8 8 0 0 1 8 8 M7 9h9 M7 13h6",
  arrow: "M5 12h14 M14 7l5 5-5 5",
  plus: "M12 5v14 M5 12h14",
  trash: "M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7",
  flow: "M4 3h6v6H4z M14 15h6v6h-6z M7 9v9h7 M10 6h7v9",
  mail: "M3 5h18v14H3z M3 5l9 8 9-8",
  send: "M22 2L9 15 M22 2l-7 20-6-7-7-6z",
  terminal: "M5 6l6 6-6 6 M13 18h7",
  shield: "M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M8 12l3 3 5-6",
  branch:
    "M6 3v12a4 4 0 0 0 4 4h8 M6 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4 M18 5v6a4 4 0 0 1-4 4H6 M18 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4",
  clip: "M8 14l7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l10-10",
  file: "M5 2h9l5 5v15H5z M14 2v6h5 M8 13h8 M8 17h6",
  close: "M6 6l12 12 M6 18L18 6",
  refresh:
    "M20 7v5h-5 M4 17v-5h5 M5 7a8 8 0 0 1 14-2l1 7 M4 12l1 7a8 8 0 0 0 14-2",
  clock: "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18 M12 7v6l4 2",
  check: "M5 12l4 4L19 6",
  external: "M14 3h7v7 M21 3L10 14 M10 3H3v18h18v-7",
  search: "M11 3a8 8 0 1 0 0 16a8 8 0 0 0 0-16 M17 17l4 4",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  chevron: "M9 5l7 7-7 7",
  spark: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z",
  activity: "M2 12h4l3-8 6 16 3-8h4",
  list: "M9 6h12 M9 12h12 M9 18h12 M3 6h1 M3 12h1 M3 18h1",
  board: "M3 4h5v16H3z M10 4h5v11h-5z M17 4h5v14h-5z",
  panel: "M3 4h18v16H3z M15 4v16",
  download: "M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5",
  folder: "M3 7V4h6l3 3h9v13H3z",
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.agents}"/></svg>`;
const badge = value => {
  const observed = outcomeState(value);
  return `<span class="badge ${escape(observed)}">${escape(statusLabel(value))}</span>`;
};
const state = {
  route: ["mcp", "skills", "agents", "settings", "workflows"].includes(location.hash.slice(1))
    ? location.hash.slice(1)
    : "workspace",
  mode: "overview",
  agents: [],
  projects: [],
  project: null,
  sessions: [],
  config: {},
  runtime: {},
  session: null,
  snapshot: { threads: [], tasks: [], messages: [], artifacts: [] },
  selectedThread: null,
  openTabs: [],
  conversations: new Map(),
  activities: new Map(),
  streams: new Map(),
  drafts: new Map(),
  events: [],
  connection: "connecting",
  settingsId: null,
  settingsDraft: null,
  settingsTab: "general",
  selectedSkill: 0,
  profilePreview: true,
  mcpServers: [],
  skillRegistry: { items: [], has_more: false, cursor: null },
  skillDetail: null,
  skillSource: "",
  skillFile: "SKILL.md",
  skillQuery: "",
  skillDirectory: "",
  mcpCatalogs: {},
  mcpId: null,
  mcpDraft: null,
  mcpDetail: null,
  mcpTab: "connection",
  mcpCalls: [],
  mcpQuery: "",
  mcpDirectory: "",
  mcpPending: null,
  mcpSaving: false,
  mcpDirty: false,
  modal: null,
  busy: false,
  cursor: 0,
  sessionEpoch: 0,
  syncGeneration: 0,
  workflowRows: [],
  resources: [],
  turnRequests: new Map(),
  forkRequests: new Map(),
  continuationRequests: new Map(),
  savingSettings: false,
  testingModelId: null,
  modelTestResult: null,
  inspectorOpen: false,
  messageFilter: "all",
  artifactQuery: "",
};
let eventSource, refreshTimer, toastTimer, modalReturnFocus, agentStates;
let sessionMessages = [];
const agentLaunch = createLaunchEditor({
  scope: "agent", getDraft: () => state.settingsDraft,
  read: draft => draft.command || [], write: (draft, command) => { draft.command = command; },
  escape, icon, render,
  purpose: "这里决定平台如何启动这个 Agent。平台会把任务交给它处理，再接收结果；任务目标在对话或工作流中填写。",
});
const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"]);
const idempotency = () => crypto.randomUUID();
const agent = (id) => state.agents.find((a) => a.id === id);
const requiresModel = (a) => a.kind === "llm" || (a.kind === "process" && a.llm_required);
const modelConfigured = (a) => !requiresModel(a) || (a.base_url?.trim() && a.model?.trim());
const thread = (id) => state.snapshot.threads.find((t) => t.id === id);
const agentIndex = (id) =>
  Math.max(
    0,
    state.agents.findIndex((a) => a.id === id),
  ) % 4;
const time = (value) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(value))
    : "";
const initials = (value) =>
  [...(value || "A")].slice(0, 2).join("").toUpperCase();
const avatar = (id, large = false) =>
  `<span class="${large ? "agent-avatar" : "agent-mini"} tone-${agentIndex(id)}">${large ? icon("agents") : escape(initials(agent(id)?.name || id))}</span>`;
const threadTasks = (id) =>
  state.snapshot.tasks.filter((t) => t.thread_id === id);
const latestTask = (id) =>
  threadTasks(id).find((t) => !terminal.has(t.state)) || threadTasks(id)[0];
const taskTitle = (task) =>
  compactTitle(task.title || task.spec?.payload?.input || task.spec?.metadata?.stage || "协作任务");

const unreadMessageCount = () => (state.snapshot.thread_activity || []).reduce((sum, item) => sum + item.unread_count, 0);
const rpc = (method, params) => api("/rpc", { method: "POST", body: { method, params } }).then(value => value.result);
const groups = () => instanceGroups(state.agents, state.snapshot);
const instanceUnread = count => count > 0 ? `<span class="instance-unread" aria-label="${count} 条未读消息" title="${count} 条未读消息">${count}</span>` : "";
function instanceCounts(group) {
  const active = group.instances.filter(instanceInUse).length;
  return `<span class="instance-status-counts"><span>${group.instances.length} 个实例</span>${active ? `<span>${active} 个使用中</span>` : ""}</span>`;
}
function sidebarInstances() {
  return groups().filter(group => group.instances.length).map(group => {
    const a = group.agent, selected = group.instances.some(item => item.thread_id === state.selectedThread);
    return `<button class="thread-link ${selected && state.route === "workspace" ? "active" : ""} ${group.instances.length > 1 ? "is-stacked" : ""}"
      data-action="agent-chat" data-id="${escape(a.id)}" data-agent-key="sidebar:${escape(a.id)}" data-focus="sidebar-agent-${escape(a.id)}">
      ${agentStatusDot(agentStates.forAgent(a.id))}${avatar(a.id)}<span class="thread-name">${escape(a.name)}${instanceCounts(group)}</span>${instanceUnread(group.unread_count)}</button>`;
  }).join("") || '<div class="thread-placeholder">选择 Agent，开始第一段对话。</div>';
}
let activityObserver = null, readTimer = null;
const pendingReads = new Map();
function observeVisibleActivity() {
  activityObserver?.disconnect();
  if (state.modal || document.visibilityState !== "visible" || state.route !== "workspace") return;
  activityObserver = new IntersectionObserver(entries => {
    if (state.modal || document.visibilityState !== "visible") return;
    for (const entry of entries) {
      if (!entry.isIntersecting || entry.intersectionRect.height < Math.min(32, entry.boundingClientRect.height)) continue;
      const { activityThread: threadId, activityPosition: position } = entry.target.dataset;
      const positions = pendingReads.get(threadId) || new Set();
      positions.add(Number(position));
      pendingReads.set(threadId, positions);
      activityObserver.unobserve(entry.target);
    }
    clearTimeout(readTimer);
    if (pendingReads.size) readTimer = setTimeout(flushVisibleReads, 150);
  }, { threshold: [0, 0.5, 1] });
  document.querySelectorAll('[data-activity-position][data-activity-unread="true"]').forEach(element => activityObserver.observe(element));
}
async function flushVisibleReads() {
  const epoch = state.sessionEpoch;
  for (const [threadId, positions] of [...pendingReads]) {
    pendingReads.delete(threadId);
    const listed = [...positions];
    try {
      for (let offset = 0; offset < listed.length; offset += 100) {
        const receipt = await rpc("threads.mark_read", { thread_id: threadId, positions: listed.slice(offset, offset + 100) });
        if (epoch !== state.sessionEpoch) continue;
        for (const item of state.activities.get(threadId)?.items || []) if (receipt.positions.includes(item.position)) item.read = true;
        for (const item of Object.values(state.snapshot.message_activity || {})) if (receipt.positions.includes(item.position)) item.read = true;
        const summary = state.snapshot.thread_activity?.find(item => item.thread_id === threadId);
        if (summary) summary.unread_count = receipt.unread_count;
      }
    } catch (error) { toast("未能保存已读状态：" + error.message, true); }
  }
  if (epoch === state.sessionEpoch) render();
}
const activityAttributes = item => item ? `data-activity-thread="${escape(item.thread_id)}" data-activity-position="${item.position}" data-activity-unread="${!item.read}"` : "";
function animateActivityMoves(previous) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (const element of document.querySelectorAll("[data-agent-key]")) {
    const old = previous.get(element.dataset.agentKey), current = element.getBoundingClientRect();
    if (old && (Math.abs(old.top - current.top) > 1 || Math.abs(old.left - current.left) > 1))
      element.animate([{ transform: `translate(${old.left - current.left}px,${old.top - current.top}px)` }, { transform: "none" }], { duration: 220, easing: "ease-out" });
  }
}
const unreadMessageBadge = () => {
  const count = unreadMessageCount();
  return count > 0 ? `<span class="message-unread-badge" aria-label="${count} 条未读消息" title="${count} 条未读消息">${count}</span>` : "";
};
function workspaceSections() {
  return [
    ["overview", "grid", "总览", 0],
    ["messages", "mail", "协作消息", sessionMessages.length],
    ["artifacts", "folder", "工作产物", state.snapshot.artifacts.length],
  ];
}

const integrationUI = createIntegrationUI({
  state,
  escape,
  icon,
  badge,
  markdown,
  api,
  toast,
  render,
  renderModal,
  idempotency,
  loadBootstrap,
  time,
});
const workflowUI = createWorkflowUI({ state, escape, icon, badge, markdown, api, toast, render, idempotency, time,
  ensureSession: async identity => { await loadBootstrap(); await selectSession(identity); } });
const agentStatusDot = (status) => `<span class="dot status-${status.tone}" title="${escape(status.hint || status.label)}" role="img" aria-label="${escape(status.label)}"></span>`;
const agentStatusBadge = (status) => `<span class="badge agent-state status-${status.tone}" title="${escape(status.hint || status.label)}">${escape(status.label)}</span>`;

function toast(message, error = false) {
  const target = $("#toast");
  target.textContent = message;
  target.className = "visible" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (target.className = ""), 4500);
}
function projectSwitcher() {
  return `<div class="project-switch"><div class="context-heading"><label for="project-select">当前项目</label><details class="context-menu" data-context="project"><summary data-focus="project-menu" aria-label="项目操作" title="项目操作">···</summary><div class="context-menu-panel"><button data-action="new-project">${icon("plus")}新建项目</button><button data-action="edit-project" ${state.project ? "" : "disabled"}>${icon("settings")}项目设置</button><button class="danger-text" data-action="delete-project" ${state.project ? "" : "disabled"}>${icon("trash")}删除当前项目</button></div></details></div><select id="project-select" aria-label="选择项目">${state.projects.map(p => `<option value="${escape(p.id)}" ${state.project?.id === p.id ? "selected" : ""}>${escape(p.name)}</option>`).join("")}</select></div>`;
}
function sessionSwitcher() {
  return `<div class="workspace-switch"><div class="context-heading"><label for="workspace-select">当前会话</label><details class="context-menu" data-context="session"><summary data-focus="session-menu" aria-label="会话操作" title="会话操作">···</summary><div class="context-menu-panel"><button data-action="new-session">${icon("plus")}新建会话</button><button class="danger-text" data-action="delete-session" ${state.session ? "" : "disabled"}>${icon("trash")}删除当前会话</button></div></details></div><select id="workspace-select" class="workspace-select" aria-label="选择会话"><option value="">选择或创建会话</option>${state.sessions.map(s => `<option value="${escape(s.id)}" ${state.session?.id === s.id ? "selected" : ""}>${escape(s.title)}${s.state !== "OPEN" ? " · 已关闭" : ""}</option>`).join("")}</select></div>`;
}
function empty(title, description, action = "", label = "") {
  return `<div class="empty">${icon("chat")}<h3>${escape(title)}</h3><div>${escape(description)}</div>${action ? `<button class="btn" data-action="${action}">${escape(label)}</button>` : ""}</div>`;
}
function focusMemory() {
  const focused = document.activeElement;
  const chat = $(".conversation"), top = chat?.getBoundingClientRect().top;
  const anchor = chat && [...chat.querySelectorAll("[data-entry-id]")].find(element => element.getBoundingClientRect().bottom > top);
  return {
    key: focused?.dataset.focus || focused?.id,
    start: focused?.selectionStart,
    end: focused?.selectionEnd,
    chat: chat && { threadId: chat.dataset.threadId, top: chat.scrollTop,
      bottom: chat.scrollHeight - chat.scrollTop - chat.clientHeight < 100,
      anchor: anchor?.dataset.entryId, offset: anchor ? anchor.getBoundingClientRect().top - top : 0 },
    page: $(".page-body")?.scrollTop,
  };
}
let renderedView = "";
function render() {
  if (workflowUI.isInteracting()) return;
  agentStates = createAgentStatusIndex(state.agents, state.snapshot, state.connection);
  sessionMessages = messageItems(state.snapshot);
  const focus = focusMemory();
  const view = [state.route, state.project?.id, state.session?.id, state.mode,
    state.selectedThread, state.settingsId, state.settingsTab, state.mcpId,
    state.mcpTab, state.skillDetail?.id, state.skillFile].join("|");
  const viewChanged = view !== renderedView;
  const previousPositions = new Map([...document.querySelectorAll("[data-agent-key]")].map(element => [element.dataset.agentKey, element.getBoundingClientRect()]));
  const composerInput = !viewChanged ? $("#chat-input") : null;
  renderedView = view;
  const openMenus = new Set(viewChanged ? [] : [...document.querySelectorAll(".context-menu[open]")].map(menu => menu.dataset.context));
  const routeHash = "#" + state.route;
  if (location.hash !== routeHash) history.replaceState(null, "", routeHash);
  if (state.modal && !$("#modal-root").firstElementChild) rememberModalOrigin();
  $("#app").innerHTML = shell();
  if (composerInput && $("#chat-input")) {
    composerInput.disabled = $("#chat-input").disabled;
    composerInput.value = state.drafts.get(state.selectedThread) || "";
    $("#chat-input").replaceWith(composerInput);
  }
  document.querySelectorAll(".context-menu").forEach(menu => { menu.open = openMenus.has(menu.dataset.context); });
  renderModal();
  workflowUI.mount();
  if (focus.key && !state.modal) {
    const target =
      document.getElementById(focus.key) ||
      [...document.querySelectorAll("[data-focus]")].find(
        (e) => e.dataset.focus === focus.key,
      );
    if (target) {
      target.focus({ preventScroll: true });
      if (
        typeof target.setSelectionRange === "function" &&
        focus.start !== null
      ) {
        try {
          target.setSelectionRange(focus.start, focus.end);
        } catch {}
      }
    }
  }
  if ($(".page-body")) $(".page-body").scrollTop = viewChanged ? 0 : focus.page || 0;
  const chat = $(".conversation");
  if (chat) {
    const previous = focus.chat;
    if (!previous || previous.threadId !== chat.dataset.threadId || previous.bottom) chat.scrollTop = chat.scrollHeight;
    else {
      const anchor = [...chat.querySelectorAll("[data-entry-id]")].find(element => element.dataset.entryId === previous.anchor);
      chat.scrollTop = anchor ? chat.scrollTop + anchor.getBoundingClientRect().top - chat.getBoundingClientRect().top - previous.offset : previous.top;
    }
  }
  requestAnimationFrame(() => { animateActivityMoves(previousPositions); observeVisibleActivity(); });
}
function shell() {
  const nav = workspaceSections();
  const label =
    state.route === "workflows"
      ? "工作流画布"
      : state.route === "agents"
      ? "Agent 集成"
      : state.route === "skills"
        ? "Skill 集成"
      : state.route === "mcp"
        ? "MCP 集成"
        : state.route === "settings"
          ? "设置"
          : state.selectedThread
            ? thread(state.selectedThread)?.title
            : nav.find(([mode]) => mode === state.mode)?.[2];
  return `<div class="layout">
    <aside class="sidebar" id="workspace-nav" aria-label="工作区导航">
      <a class="sidebar-brand" href="#" data-action="mode" data-mode="overview" aria-label="Orchestrate Everything with Agents" title="Orchestrate Everything with Agents"><img class="brand-mark" src="/assets/mark.svg?v=4" alt=""><span class="brand-wordmark"><strong>OEA</strong><small>个人协作工作台</small></span></a>
      ${projectSwitcher()}
      ${sessionSwitcher()}
      <button class="search-launcher" data-action="command">${icon("search")}<span>搜索或快速跳转</span><kbd>Ctrl K</kbd></button>
      <div class="section-label">工作区 <span>WORKSPACE</span></div>
      <nav class="side-navigation" aria-label="功能导航"><button class="side-nav ${state.route === "workspace" ? "active" : ""}" data-action="mode" data-mode="overview" ${state.route === "workspace" ? 'aria-current="page"' : ""}>${icon("grid")}<span>工作台</span>${unreadMessageBadge()}</button><button class="side-nav ${state.route === "workflows" ? "active" : ""}" data-action="route" data-route="workflows" ${state.route === "workflows" ? 'aria-current="page"' : ""}>${icon("flow")}<span>工作流</span></button><button class="side-nav ${state.route === "agents" ? "active" : ""}" data-action="route" data-route="agents" ${state.route === "agents" ? 'aria-current="page"' : ""}>${icon("agents")}<span>Agent</span><span class="nav-tag">${state.agents.filter((a) => a.kind !== "external").length}</span></button><button class="side-nav ${state.route === "mcp" ? "active" : ""}" data-action="route" data-route="mcp" ${state.route === "mcp" ? 'aria-current="page"' : ""}>${icon("plug")}<span>MCP 工具</span><span class="nav-tag">${state.mcpServers.length}</span></button><button class="side-nav ${state.route === "skills" ? "active" : ""}" data-action="route" data-route="skills" ${state.route === "skills" ? 'aria-current="page"' : ""}>${icon("book")}<span>Skills</span><span class="nav-tag">${state.skillRegistry.items.length}</span></button></nav>
      <div class="section-label">独立对话 <button class="icon-btn" data-action="open-agent" aria-label="打开 Agent 对话" title="打开 Agent 对话">${icon("plus")}</button></div>
      <div class="thread-list">${sidebarInstances()}</div>
      <div class="sidebar-footer"><button class="side-nav" data-action="open-trash">${icon("trash")}<span>回收站</span></button><button class="side-nav ${state.route === "settings" ? "active" : ""}" data-action="route" data-route="settings">${icon("settings")}<span>设置与配置</span></button><div class="local-status"><span class="dot ${state.connection === "online" ? "online" : "offline"}"></span>本地工作区<span>LOCAL</span></div></div>
    </aside>
    <main class="main" id="main-content" tabindex="-1"><header class="topbar"><div class="breadcrumb"><span class="breadcrumb-root">${escape(state.project?.name || "工作区")}</span>${icon("chevron")}<strong>${escape(label || "会话总览")}</strong>${state.session ? `<span class="breadcrumb-divider"></span><span class="breadcrumb-session">${escape(state.session.title)}</span>` : ""}</div><div class="top-actions"><div class="connection"><span class="dot ${state.connection === "online" ? "online" : "offline"}"></span><span class="connection-text">${state.connection === "online" ? "本地连接正常" : state.connection === "connecting" ? "正在连接" : "正在重新连接"}</span></div><button class="icon-btn top-search" data-action="command" aria-label="搜索与快捷操作" title="搜索与快捷操作 · Ctrl K">${icon("search")}</button><span class="topbar-divider"></span><button class="btn" data-action="new-session">${icon("plus")}<span>新建会话</span></button></div></header>
      ${state.connection === "offline" ? '<div class="connection-banner">与本地服务的连接中断，正在重连。已接收的任务会继续执行。<button class="text-link" data-action="reconnect">重新连接</button></div>' : ""}
      ${state.route === "workspace" ? tabbar() : ""}
      <div class="page-body">${state.route === "workflows" ? workflowUI.page() : state.route === "mcp" ? integrationUI.mcpPage() : state.route === "skills" ? integrationUI.skillPage() : state.route === "settings" ? settings() : state.route === "agents" ? agentsPage() : !state.session ? welcome() : state.selectedThread ? chat() : state.mode === "messages" ? messagesPage() : state.mode === "artifacts" ? artifactsPage() : overview()}</div>
    </main></div>`;
}
function tabbar() {
  return `<nav class="tabbar" aria-label="总览与协作标签页">${workspaceSections().map(([mode, symbol, title, count]) =>
    `<button class="browser-tab workspace-tab ${!state.selectedThread && state.mode === mode ? "active" : ""}" data-action="mode" data-mode="${mode}" data-focus="workspace-tab-${mode}" ${!state.selectedThread && state.mode === mode ? 'aria-current="page"' : ""} ${!state.session && mode !== "overview" ? "disabled" : ""}>${icon(symbol)}<span class="tab-name">${title}</span>${count ? `<span class="tab-count">${count}</span>` : ""}</button>`).join("")}${state.openTabs.length ? '<span class="tab-separator" aria-hidden="true"></span>' : ""}${state.openTabs
    .map((id) => thread(id))
    .filter(Boolean)
    .map(
      (t) =>
        `<div class="browser-tab ${state.selectedThread === t.id ? "active" : ""}"><button data-action="select-thread" data-id="${escape(t.id)}" class="split">${avatar(t.agent_id)}<span class="tab-name">${escape(t.title)}</span></button><button class="tab-close" data-action="close-tab" data-id="${escape(t.id)}" aria-label="关闭 ${escape(t.title)} 分页">×</button></div>`,
    )
    .join(
      "",
    )}<button class="tab-plus" data-action="open-agent" aria-label="新建 Agent 对话" ${!state.session ? "disabled" : ""}>+</button></nav>`;
}
function readyAgents() {
  return state.agents.filter(a => a.kind !== "external" && a.enabled && (!requiresModel(a) || modelConfigured(a)));
}
function workGuide() {
  return `<details class="work-guide"><summary>如何开始</summary><p>选择 Agent，说明目标和交付要求。提交后在任务中查看进展，需要补充信息时会提示你；结果和文件保存在当前会话。</p></details>`;
}
function nextStep() {
  const ready = readyAgents(), existing = state.agents.find(a => a.kind !== "external");
  const attention = state.snapshot.tasks.find(t => t.state === "WAITING" && t.wait?.kind === "input") || state.snapshot.tasks.find(t => matchesTaskFilter(t, "attention"));
  const title = attention ? attention.state === "WAITING" ? "补充信息，让工作继续" : "先核对需要关注的任务" : !existing ? "先添加你的第一个 Agent" : !ready.length ? "完成 Agent 配置" : !state.session ? "为目标创建一个会话" : "团队已就绪";
  const description = attention ? escape(taskTitle(attention)) : !existing ? "连接模型或本机执行端，建立你的工作伙伴。" : !ready.length ? "启用 Agent，并填写模型地址与名称。保存后可测试连接。" : !state.session ? "会话用于集中保存同一目标的对话、任务与成果。" : "选择 Agent 开始对话，或运行已保存的工作流。";
  const action = attention ? "task-detail" : !existing ? "add-agent" : !ready.length ? "configure-agent" : !state.session ? "new-session" : "open-agent";
  const label = attention ? attention.state === "WAITING" ? "提供输入" : "查看任务" : !existing ? "添加 Agent" : !ready.length ? "去配置" : !state.session ? "创建会话" : "开始对话";
  return `<div class="next-step ${attention ? "needs-attention" : ""}"><span class="next-step-icon">${icon(attention ? "activity" : "spark")}</span><div class="next-step-copy"><h2>${title}</h2><p>${description}</p></div><div class="next-step-actions"><button class="btn primary" data-action="${action}" ${attention ? `data-id="${escape(attention.id)}"` : action === "configure-agent" ? `data-id="${escape(existing.id)}" data-tab="general"` : ""}>${label}${icon("arrow")}</button>${ready.length && state.session ? '<button class="btn" data-action="route" data-route="workflows">选择工作流</button>' : ""}</div></div>`;
}
function welcome() {
  return `<section class="overview"><div class="page-heading"><div><h1>从一个目标开始</h1><p>准备 Agent，创建会话，再把工作交给它。每一步进展都可查看。</p></div></div>${nextStep()}<div class="section-heading"><h2>你的 Agent</h2><div class="section-actions">${workGuide()}<button class="text-link" data-action="route" data-route="agents">管理 Agent ${icon("arrow")}</button></div></div><div class="agent-grid">${agentCards()}</div></section>`;
}
function agentCards() {
  return groups().map(group => {
    const a = group.agent, ready = a.enabled && (!requiresModel(a) || modelConfigured(a));
    const description = a.system_prompt || a.agent_md || (a.kind === "mock" ? "本机协议验证，不调用模型。" : a.kind === "process" ? "由本机执行程序处理任务。" : "独立上下文，使用已授权的工具执行任务。");
    const recent = group.instances[0];
    return `<article class="agent-card tone-${agentIndex(a.id)} ${group.instances.length > 1 ? "is-stacked" : ""}" data-agent-key="overview:${escape(a.id)}">
      <div class="agent-card-head">${avatar(a.id, true)}<div class="agent-card-identity"><h3 class="agent-card-name" title="${escape(a.name)}">${escape(a.name)}</h3><div class="agent-card-model">${escape(a.model || (a.kind === "mock" ? "本机验证执行端" : a.kind === "process" ? "本机程序" : "等待配置模型"))}</div></div>${agentStatusBadge(agentStates.forAgent(a.id))}${instanceUnread(group.unread_count)}</div>
      ${instanceCounts(group)}<div class="agent-description ${recent?.summary_markdown ? "agent-message-preview" : ""}">${markdown(recent?.summary_markdown || description.slice(0, 120))}</div>${integrationUI.agentDocumentLinks(a)}
      <div class="agent-card-detail"><button class="icon-btn" data-action="configure-agent" data-id="${escape(a.id)}" data-tab="general" title="配置 ${escape(a.name)}" aria-label="配置 ${escape(a.name)}">${icon("settings")}</button><button class="btn small ${ready ? "" : "primary"}" data-action="${ready ? "agent-chat" : "configure-agent"}" data-id="${escape(a.id)}" data-focus="overview-agent-${escape(a.id)}" data-tab="general">${ready ? group.instances.length > 1 ? "选择实例" : "打开对话" : "完成配置"}${icon("arrow")}</button></div></article>`;
  }).join("") || empty("还没有 Agent", "添加 Agent 后，就能为它配置模型、工作指令与工具。", "add-agent", "添加 Agent");
}
function overview() {
  const tasks = state.snapshot.tasks;
  const attention = tasks.filter(t => matchesTaskFilter(t, "attention"));
  return `<section class="overview"><div class="page-heading"><div><h1>今天，推进什么？</h1><p>先明确目标，再选择 Agent。执行进度和成果会保存在此会话。</p></div><button class="btn" data-action="workflow">${icon("flow")}安排依赖任务</button></div><div class="workspace-brief">${nextStep()}<div class="workspace-summary"><span><strong>${readyAgents().length}</strong> 个可用 Agent</span><span class="summary-running"><strong>${tasks.filter(t => t.state === "RUNNING").length}</strong> 项执行中</span><span class="summary-attention"><strong>${attention.length}</strong> 项需要关注</span><span class="summary-done"><strong>${tasks.filter(t => matchesTaskFilter(t, "done")).length}</strong> 项已完成</span></div></div><div class="section-heading"><h2>你的 Agent</h2><div class="section-actions">${workGuide()}<button class="text-link" data-action="route" data-route="agents">管理 Agent ${icon("arrow")}</button></div></div><div class="agent-grid">${agentCards()}</div><div class="workspace-history"><section class="recent-tasks"><div class="section-heading"><h2>最近任务</h2></div>${tasks.length ? `<div class="activity-list">${tasks.slice(0, 5).map(t => `<button class="activity-row" data-action="task-detail" data-id="${escape(t.id)}">${avatar(t.spec?.agent_id)}<span class="activity-name">${escape(taskTitle(t))}<small>${escape(agent(t.spec?.agent_id)?.name || "Agent")}</small></span>${badge(t)}<time>${time(t.created_at)}</time>${icon("chevron")}</button>`).join("")}</div>` : empty("发起第一项工作", "打开 Agent 对话，写下目标和期望的交付内容。", "open-agent", "开始对话")}</section><section class="session-updates">${attention.length ? `<div class="section-heading"><h2>需要你处理</h2></div><div class="attention-list">${attention.slice(0, 4).map(t => `<button class="attention-item ${outcomeState(t)}" data-action="task-detail" data-id="${escape(t.id)}"><div><strong>${escape(taskTitle(t))}</strong><small>${t.state === "WAITING" ? t.wait?.kind === "input" ? "提供输入，继续执行" : "等待协作，可查看进度" : "查看状态说明并核对结果"}</small></div>${badge(t)}${icon("arrow")}</button>`).join("")}</div>` : ""}${recentArtifacts()}</section></div></section>`;
}
function chat() {
  const t = thread(state.selectedThread);
  if (!t) return overview();
  const a = agent(t.agent_id),
    tasks = threadTasks(t.id),
    current = latestTask(t.id);
  const conversation = state.conversations.get(t.id);
  const pending = state.snapshot.messages.filter(
    (m) => m.target_thread_id === t.id && m.state === "PENDING",
  );
  return `<div class="chat-layout ${state.inspectorOpen ? "show-inspector" : ""}"><section class="chat-panel"><header class="chat-header">${avatar(t.agent_id, true)}<div><h2>${escape(t.title)}</h2><p>${escape(a?.model || (a?.kind === "mock" ? "Mock · 本地执行" : "独立执行端"))} · 上下文版本 ${t.revision}</p></div><div class="chat-header-actions"><button class="icon-btn" data-action="toggle-inspector" title="执行详情" aria-label="切换执行详情" aria-expanded="${state.inspectorOpen}">${icon("panel")}</button><button class="btn small" data-action="compose-message">${icon("mail")}协作消息</button><button class="icon-btn" data-action="fork" data-id="${escape(t.id)}" title="从最近保存的进度创建分支" aria-label="创建对话分支" ${!t.latest_checkpoint_id || !t.adapter_snapshot?.supports_fork ? "disabled" : ""}>${icon("branch")}</button></div></header>
    <div class="conversation" data-thread-id="${escape(t.id)}" aria-label="对话记录">${conversation?.has_more || state.activities.get(t.id)?.has_more ? '<button class="btn small load-more" data-action="older">加载更早的消息</button>' : ""}${!conversation ? '<div class="loading-inline">正在读取对话记录…</div>' : conversationItems(conversation, state.activities.get(t.id)).length ? conversationItems(conversation, state.activities.get(t.id)).map(entry => entry.notice ? threadNotice(entry.activity) : message(entry, t)).join("") : empty("这是一个独立的对话", "向这个 agent 描述目标，或启动待应用的协作消息。")}
      <div id="live-messages">${liveMessages(t)}</div>
      ${current?.state === "WAITING" ? `<div class="storage-note">${icon("clock")}<div>任务已暂停并保存进度，可在补充信息后继续。${current.wait?.kind === "input" ? '<button class="text-link" data-action="task-detail" data-id="' + escape(current.id) + '">提供输入并恢复 →</button>' : current.wait?.kind === "message" ? "等待协作消息到达。" : "等待子任务完成。"}</div></div>` : ""}
      ${current?.state === "UNCERTAIN" ? `<div class="storage-note">${icon("shield")}<div>执行结果需要核对，相关上下文和资源保持隔离。<button class="text-link" data-action="task-detail" data-id="${escape(current.id)}">查看待核对任务 →</button></div></div>` : ""}
    </div><div class="composer-area"><form id="composer" class="composer"><textarea id="chat-input" data-focus="chat-input" aria-label="发送给 ${escape(t.title)} 的消息" placeholder="向 ${escape(a?.name || t.title)} 发送指令…" rows="2" ${state.session.state !== "OPEN" ? "disabled" : ""}>${escape(state.drafts.get(t.id) || "")}</textarea><div class="composer-footer"><div class="composer-tools"><button type="button" class="icon-btn" data-action="upload" title="添加工作产物" aria-label="上传产物">${icon("clip")}</button><span>${pending.length ? `${pending.length} 条协作消息待应用` : "Enter 发送 · Shift + Enter 换行"}</span>${pending.length ? '<button type="button" class="text-link" data-action="process-messages">处理消息</button>' : ""}</div>${current?.state === "RUNNING" ? `<button type="button" class="btn small" data-action="cancel-task" data-id="${escape(current.id)}">停止当前任务</button>` : ""}<button type="submit" class="btn primary" ${state.busy || state.session.state !== "OPEN" ? "disabled" : ""}>发送 ${icon("send")}</button></div></form><div class="composer-note">${tasks.some((t) => t.state === "RUNNING") ? "执行期间的新指令会排队，在下一次调用时读取。" : "关闭分页后，任务和对话仍会保留。"}</div></div></section>
    <aside class="inspector" aria-label="执行详情"><button class="icon-btn inspector-close" data-action="toggle-inspector" aria-label="关闭执行详情">${icon("close")}</button><div class="inspector-section"><h3>${icon("flow")}执行上下文</h3><div class="kv"><span>当前状态</span>${current ? badge(current) : "<strong>准备就绪</strong>"}</div><div class="kv"><span>上下文版本</span><strong>${t.revision}</strong></div><div class="kv"><span>待应用消息</span><strong>${pending.length}</strong></div><div class="kv"><span>对话标识</span><code title="${escape(t.id)}">${escape(t.id)}</code></div><div class="kv"><span>执行端</span><strong>${escape(a?.kind || "custom")}</strong></div></div>
      <div class="inspector-section"><h3>${icon("clock")}任务记录</h3>${
        tasks
          .slice(0, 8)
          .map(
            (task) =>
              `<button class="task-mini" data-action="task-detail" data-id="${escape(task.id)}"><span class="title">${escape(taskTitle(task))}</span>${badge(task)}</button>`,
          )
          .join("") || '<p class="form-hint">指令会在这里形成独立任务。</p>'
      }</div>
      <div class="inspector-section"><h3>${icon("mail")}协作动态</h3>${
        state.events
          .filter((e) => e.thread_id === t.id)
          .slice(-8)
          .reverse()
          .map(
            (e) =>
              `<div class="timeline-item">${escape(eventLabel(e))}<time>${time(e.created_at)}</time></div>`,
          )
          .join("") ||
        '<p class="form-hint">消息投递和执行变化会在这里更新。</p>'
      }</div>
    </aside></div>`;
}
function threadNotice(item) {
  const report = item.kind === "report";
  const label = report ? item.wait_for_guidance && !item.reply_message_id ? "等待指导" : item.reply_message_id ? "已回复指导" : "Agent 报告"
    : ({ PENDING: "待应用", DELIVERED: "已入箱", OFFERED: "已提供执行端", APPLIED: "已应用", REJECTED: "已拒绝", EXPIRED: "已过期" }[item.delivery_stage] || "协作消息");
  return `<article class="message activity-notice" data-entry-id="${escape(item.source_id)}" ${activityAttributes(item)}>${icon(report ? "activity" : "mail")}
    <div class="message-content"><div class="message-meta"><strong>${label}</strong><time>${time(item.created_at)}</time></div>
    <div class="message-text">${markdown(item.summary_markdown)}</div>
    ${item.task_id ? `<button class="text-link" data-action="task-detail" data-id="${escape(item.task_id)}">查看执行详情</button>` : ""}</div></article>`;
}
function message(entry, t) {
  const user = entry.role === "user";
  const task = state.snapshot.tasks.find(task => task.id === entry.task_id);
  const observed = entry.execution_state ? entry : task;
  return `<article class="message ${user ? "user" : ""}" data-entry-id="${escape(entry.id)}" ${activityAttributes(entry.activity)}>${user ? '<span class="agent-mini">你</span>' : avatar(t.agent_id)}<div class="message-content"><div class="message-meta"><strong>${user ? "你" : escape(agent(t.agent_id)?.name || t.title)}</strong>${observed ? badge(observed) : ""}<time>${time(entry.created_at)}</time></div><div class="message-text">${markdown(entry.content)}</div>${(entry.metadata?.artifact_ids || []).map((id) => `<a class="attachment-link" href="/api/artifacts/${encodeURIComponent(id)}/download">${icon("file")}下载产物</a>`).join("")}${!user && entry.task_id ? `<button class="text-link message-execution" data-action="task-detail" data-id="${escape(entry.task_id)}">${entry.metadata?.acceptance?.state === "FAILED" ? "结果未完整交付 · 查看原因" : "执行详情"}</button>` : ""}${!user && entry.metadata?.checkpoint_id && t.adapter_snapshot?.supports_fork ? `<button class="text-link message-execution" data-action="fork" data-id="${escape(entry.thread_id)}" data-checkpoint="${escape(entry.metadata.checkpoint_id)}">从此处分支</button>` : ""}</div></article>`;
}
function liveMessages(t) {
  const live = [...state.streams.values()].filter(stream => stream.thread_id === t.id);
  if (live.length) return live.map(stream => streamMessage(stream, t)).join("");
  const tasks = threadTasks(t.id);
  if (tasks.some(task => task.state === "RUNNING")) return streamMessage({ text: "", tool: "" }, t);
  if (tasks.some(task => task.state === "QUEUED")) return '<div class="loading-inline" role="status">指令已收到，等待执行…</div>';
  return "";
}
function streamMessage(stream, t) {
  return `<article class="message streaming ${stream.text ? "" : "thinking"}">${avatar(t.agent_id)}<div class="message-content"><div class="message-meta"><strong>${escape(agent(t.agent_id)?.name || t.title)}</strong><span>${escape(statusLabel("RUNNING"))}</span></div>${stream.text ? `<div class="message-text">${markdown(stream.text)}</div>` : ""}<div class="stream-label" role="status"><span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>${escape(stream.tool || (stream.text ? "正在输出" : "正在思考…"))}</div></div></article>`;
}
function eventLabel(event) {
  if (["task.completed", "task.failed", "task.cancelled", "task.skipped", "task.suspended", "task.uncertain"].includes(event.type)) {
    const task = state.snapshot.tasks.find(item => item.id === event.task_id);
    return task ? statusLabel(task) : "执行状态已更新";
  }
  return (
    {
      "task.accepted": "指令已接收",
      "run.started": "开始执行",
      "task.resumed": "恢复执行",
      "message.accepted": "协作消息已接收",
      "message.offered": "消息已交付执行端",
      "message.applied": "消息消费回执已保存",
      "task.reconciled": "已保存核对处理",
    }[event.type] || event.type
  );
}

function messageCard(m, compact = false) {
  const report = m.kind === "report", source = report || m.envelope?.source_run_id ? thread(m.source_thread_id) : null,
    taskId = report ? m.task_id : m.envelope?.source_task_id;
  return `<article class="message-card ${compact ? "feedback-card" : ""}" ${activityAttributes(state.snapshot.message_activity?.[m.id])}><div class="message-route">${source ? avatar(source.agent_id) : icon("terminal")}<strong>${escape(source?.title || "外部协调者")}</strong>${report ? `<span class="message-kind">Agent 报告</span>` : `${icon("arrow")}<strong>${escape(thread(m.target_thread_id)?.title || m.target_thread_id)}</strong>`}${badge(m.state)}</div><div class="message-text markdown">${markdown(m.summary_markdown)}</div><footer><span>${time(m.created_at)}${report ? " · 报告摘要" : ` · ${escape(m.target_port)} · ${escape(m.envelope?.message_type || "")}`}</span><div class="message-actions">${taskId ? `<button class="text-link" data-action="task-detail" data-id="${escape(taskId)}">查看任务 ${icon("arrow")}</button>` : ""}${!compact && !report && m.state === "PENDING" ? `<button class="text-link" data-action="process-target" data-id="${escape(m.target_thread_id)}">启动接收任务 ${icon("arrow")}</button>` : ""}</div></footer></article>`;
}
function artifactSourceLabel(item) {
  const source = artifactSource(item, state.agents);
  return source.kind === "agent" ? `来源 Agent：${source.label}` : source.label;
}
function recentArtifacts() {
  const items = state.snapshot.artifacts.slice(0, 5);
  return `<section class="recent-artifacts"><div class="section-heading"><h2>最近产物</h2><button class="text-link" data-action="mode" data-mode="artifacts">查看全部 ${icon("arrow")}</button></div>${items.length ? `<div class="recent-artifact-list">${items.map(a => `<div class="recent-artifact-row"><button class="recent-artifact-open" data-action="artifact-preview" data-id="${escape(a.id)}" data-focus="artifact-${escape(a.id)}" aria-label="查看原文：${escape(a.name)}">${icon("file")}<span><strong>${escape(a.name)}</strong><small>${escape(artifactSourceLabel(a))}</small><small>${formatBytes(a.size)} · ${time(a.created_at)}</small></span>${icon("chevron")}</button><a class="icon-btn" href="${artifactDownload(a.id)}" aria-label="下载 ${escape(a.name)}" title="下载产物">${icon("download")}</a></div>`).join("")}</div>` : `<p class="quiet-state">${icon("file")}Agent 交付成果或你上传文件后，会在这里显示。<button class="text-link" data-action="upload">添加产物</button></p>`}</section>`;
}
function messagesPage() {
  const all = sessionMessages, messages = all.filter(m => matchesMessageFilter(m, state.messageFilter));
  return `<section class="panel-page"><div class="page-heading"><div><p class="eyebrow">CONNECTED CONTEXTS</p><h1>协作消息<span class="heading-dot">.</span></h1><p>让独立的思考彼此连接，每一次交接都有回音。</p></div><button class="btn primary" data-action="compose-message">${icon("send")}发送消息</button></div><div class="filter-toolbar"><div class="filter-tabs" aria-label="消息状态筛选">${[
    ["all", "全部消息"],
    ["reports", "报告与指导"],
    ["PENDING", "待应用"],
    ["OFFERED", "已交付"],
    ["APPLIED", "已应用"],
  ]
    .map(
      ([filter, title]) =>
        `<button class="filter-tab ${state.messageFilter === filter ? "active" : ""}" data-action="message-filter" data-filter="${filter}" aria-pressed="${state.messageFilter === filter}">${title}<span>${all.filter(m => matchesMessageFilter(m, filter)).length}</span></button>`,
    )
    .join(
      "",
    )}</div></div>${messages.length ? messages.map(m => messageCard(m)).join("") : empty(state.messageFilter === "all" ? "好的协作，从分享信息开始" : "此状态下暂无消息", state.messageFilter === "all" ? "把发现、建议或文件引用，传递给同一会话中的 Agent。" : "切换状态，查看其他协作消息。", "compose-message", "发送协作消息")}</section>`;
}
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function artifactCard(a) {
  return `<article class="artifact-card"><button class="artifact-card-open" data-action="artifact-preview" data-id="${escape(a.id)}" data-focus="artifact-${escape(a.id)}" aria-label="查看原文：${escape(a.name)}"><span class="file-symbol">${icon("file")}</span><h3 title="${escape(a.name)}">${escape(a.name)}</h3><p>${formatBytes(a.size)}<span>·</span>${time(a.created_at)}</p><span class="artifact-open-hint">查看原文 ${icon("arrow")}</span></button><a class="icon-btn artifact-download" href="${artifactDownload(a.id)}" aria-label="下载 ${escape(a.name)}" title="下载产物">${icon("download")}</a></article>`;
}
function artifactResults() {
  const query = state.artifactQuery.trim().toLocaleLowerCase();
  const items = state.snapshot.artifacts.filter(a => {
    const source = artifactSource(a, state.agents);
    return `${a.name} ${source.label} ${source.agent_id || ""}`.toLocaleLowerCase().includes(query);
  });
  return items.length ? `<div class="artifacts-by-agent">${groupArtifacts(items, state.agents).map(({ source, items }) => `<section class="artifact-source-group" data-source-key="${escape(source.key)}"><div class="section-heading"><h2>${source.kind === "agent" ? avatar(source.agent_id) : icon(source.kind === "upload" ? "clip" : "file")}${escape(source.label)}</h2><span class="artifact-group-count">${items.length} 项产物</span></div><div class="artifact-grid">${items.map(artifactCard).join("")}</div></section>`).join("")}</div>` : empty(state.artifactQuery ? "没有匹配的产物" : "还没有工作产物", state.artifactQuery ? "试试文件名或来源 Agent 的名称。" : "Agent 交付的成果和你上传的文件会保存在当前会话。", "upload", "添加产物");
}
function artifactsPage() {
  return `<section class="panel-page"><div class="page-heading"><div><h1>工作产物</h1><p>按来源 Agent 整理当前会话的成果，点开文件可查看原文。</p></div><button class="btn primary" data-action="upload">${icon("plus")}添加产物</button></div><div class="filter-toolbar"><span class="artifact-total">${state.snapshot.artifacts.length} 个文件 <span>· ${formatBytes(state.snapshot.artifacts.reduce((sum, a) => sum + a.size, 0))}</span></span><label class="inline-search">${icon("search")}<input id="artifact-search" data-focus="artifact-search" value="${escape(state.artifactQuery)}" placeholder="搜索文件或来源 Agent…" aria-label="搜索文件或来源 Agent"></label></div><div id="artifact-results">${artifactResults()}</div><div class="storage-note">${icon("shield")}<div>文件保存在本机。用户上传、外部提交和历史来源未记录的产物单独列出。</div></div></section>`;
}
function agentsPage() {
  return `<section class="overview"><p class="eyebrow">CONNECTED AGENTS</p><div class="page-heading"><div><h1>管理你的 Agent</h1><p>先连接模型，再补充工作指令和工具。配置完成后即可开始对话。</p></div><button class="btn primary" data-action="add-agent">${icon("plus")}添加 Agent</button></div><div class="agent-grid">${agentCards()}</div><details class="codex-guide"><summary>${icon("terminal")}外部协调 · Codex</summary><p>Codex 在自身客户端执行，通过平台接口协调子任务。</p><button class="text-link" data-action="codex">连接 Codex ${icon("arrow")}</button></details></section>`;
}
function ensureSettings() {
  if (!state.settingsDraft) {
    const existing =
      agent(state.settingsId) ||
      state.agents.find((a) => a.kind !== "external");
    state.settingsId = existing?.id || null;
    state.settingsDraft = existing
      ? { ...structuredClone(existing), api_key: "" }
      : {
          id: "",
          name: "",
          project_id: state.project?.id,
          kind: "llm",
          enabled: true,
          llm_required: true,
          base_url: "",
          model: "",
          api_key: "",
          system_prompt: "",
          temperature: null,
          reasoning_effort: null,
          max_output_tokens: 32768,
          token_parameter: "max_completion_tokens",
          max_concurrency: 4,
          timeout_seconds: 120,
          collaboration: true,
          agent_md: "",
          skills: [],
          mcp_bindings: [],
        };
  }
}
function field(
  label,
  name,
  value,
  {
    type = "text",
    placeholder = "",
    full = false,
    hint = "",
    readonly = false,
  } = {},
) {
  return `<label class="field ${full ? "full" : ""}">${label}<input data-setting="${name}" data-focus="setting-${name}" type="${type}" ${type === "number" ? `step="${name === "temperature" ? "any" : "1"}"` : ""} value="${escape(value ?? "")}" placeholder="${escape(placeholder)}" ${readonly ? "readonly" : ""} autocomplete="${type === "password" ? "new-password" : "off"}">${hint ? `<small>${hint}</small>` : ""}</label>`;
}
function modelSetupNotice(d) {
  const result = state.modelTestResult?.id === d.id ? state.modelTestResult : null;
  return `<div class="model-setup-note ${modelConfigured(d) ? "" : "pending"}"><div><strong>${modelConfigured(d) ? "模型参数已填写" : "请完成模型配置"}</strong><p>${modelConfigured(d) ? "保存后可测试接口连接；新执行会使用当前配置。无需认证的本地接口可留空密钥。" : "填写 API 地址、模型和所需密钥。保存后，等待模型配置的任务会继续执行。"}</p>${d.kind === "process" ? "<p>模型调用由平台统一管理，密钥保存在本机。修改配置无需改动 Agent 代码。</p>" : ""}</div><button type="button" class="btn small" data-action="test-model" ${!state.settingsId || state.testingModelId || state.savingSettings ? "disabled" : ""}>${state.testingModelId === d.id ? "正在测试…" : "测试已保存配置"}</button></div>${result ? `<p class="model-test-result ${result.ok ? "success" : "failure"}" role="status">${escape(result.message)}</p>` : ""}`;
}
function agentGeneral(d) {
  return `<section class="form-section"><h3>基本信息</h3><div class="form-grid">${field("显示名称", "name", d.name, { placeholder: "为这个 agent 命名" })}${field("Agent ID", "id", d.id, { readonly: !!state.settingsId, placeholder: "小写字母、数字和连字符" })}<label class="field">执行方式<select data-setting="kind" data-focus="setting-kind"><option value="llm" ${d.kind === "llm" ? "selected" : ""}>模型 API（兼容 OpenAI）</option><option value="mock" ${d.kind === "mock" ? "selected" : ""}>本机协议验证（无模型）</option><option value="process" ${d.kind === "process" ? "selected" : ""}>本机 Agent 程序</option></select></label>${field("并发上限", "max_concurrency", d.max_concurrency, { type: "number", hint: "同一对话每次只执行一个任务。" })}</div></section>
    ${d.kind === "process" ? `<section class="form-section"><h3>模型接入</h3><label class="toggle-row"><span>使用平台统一模型<small>此执行端需要模型推理时开启；纯文件处理或计算可以关闭。</small></span><input type="checkbox" data-setting="llm_required" data-focus="setting-llm_required" ${d.llm_required ? "checked" : ""}></label></section>` : ""}
    ${requiresModel(d) ? `<section class="form-section"><h3>模型 API</h3>${modelSetupNotice(d)}<div class="form-grid">${field("API 地址", "base_url", d.base_url, { placeholder: "https://…/v1", hint: "平台在此地址后附加 /chat/completions。" })}${field("模型名称", "model", d.model, { placeholder: "填写供应商支持的模型 ID" })}${field("API 密钥", "api_key", d.api_key, { type: "password", full: true, placeholder: d.has_api_key ? "已设置 · 留空保留已有密钥" : "仅写入本机加密存储", hint: "密钥不会返回浏览器，不保存在对话、事件或源码中。" })}</div><details class="form-advanced"><summary>模型高级参数</summary><div class="form-grid">${field("温度（可选）", "temperature", d.temperature, { type: "number", placeholder: "不指定，由供应商决定" })}${field("最大输出 token", "max_output_tokens", d.max_output_tokens, { type: "number" })}<label class="field">推理强度（可选）<select data-setting="reasoning_effort" data-focus="setting-reasoning_effort">${[["", "供应商默认"], ["none", "关闭"], ["minimal", "极低"], ["low", "低"], ["medium", "中"], ["high", "高"], ["xhigh", "很高"], ["max", "最大"]].map(([value, label]) => `<option value="${value}" ${(d.reasoning_effort ?? "") === value ? "selected" : ""}>${label}</option>`).join("")}</select><small>仅用于支持 reasoning_effort 的接口；未知时留空。</small></label><label class="field">输出上限参数<select data-setting="token_parameter" data-focus="setting-token_parameter"><option value="max_completion_tokens" ${d.token_parameter === "max_completion_tokens" ? "selected" : ""}>max_completion_tokens</option><option value="max_tokens" ${d.token_parameter === "max_tokens" ? "selected" : ""}>max_tokens</option></select><small>按供应商接口要求选择。</small></label>${field("模型上下文上限（可选）", "context_window_tokens", d.context_window_tokens, { type: "number", placeholder: "未知时留空", hint: "按模型规格填写；留空时使用保守预算，历史内容由平台自动管理。" })}${field("API 超时（秒）", "timeout_seconds", d.timeout_seconds, { type: "number" })}</div></details></section>` : `<div class="storage-note">${icon("shield")}<div>${d.kind === "mock" ? "此执行端在本机生成确定性回复，支持消息回执、checkpoint 和挂起恢复，无需密钥。" : "进程命令由本机配置管理，平台只终止自身创建的进程树。"}</div></div>`}
    ${d.kind === "process" ? agentLaunch.panel(d) : ""}<section class="form-section"><h3>行为与协作</h3><details class="form-advanced"><summary>编辑系统指令</summary><label class="field">系统指令<textarea data-setting="system_prompt" data-focus="setting-system_prompt" placeholder="定义执行端的工作方式与输出要求。">${escape(d.system_prompt)}</textarea></label></details><div class="instruction-preview">${content(d.system_prompt || "尚未添加系统指令。可在“工作指令”中定义职责与交付要求。")}</div><label class="toggle-row"><span>启用协作工具<small>允许同空间消息、子任务提交和 checkpoint 等待。</small></span><input type="checkbox" data-setting="collaboration" ${d.collaboration ? "checked" : ""}></label><label class="toggle-row"><span>启用 Agent<small>关闭后停止新派发，保留历史记录。</small></span><input type="checkbox" data-setting="enabled" ${d.enabled ? "checked" : ""}></label></section>`;
}
function workbenchSettings(d) {
  const workbench = d.workbench || {};
  const labels = { read_file: "读取文本", search_files: "搜索文件", write_file: "写入文件", edit_file: "修改文本", exec_command: "执行命令", read_image: "读取图片", publish_artifact: "交付产物" };
  return `<section class="form-section"><h3>工作能力</h3><div class="form-grid"><label class="field">执行环境<select id="workbench-environment"><option value="">未分配</option>${(state.environments || []).filter(environment => environment.enabled).map(environment => `<option value="${escape(environment.id)}" ${workbench.environment_id === environment.id ? "selected" : ""}>${escape(environment.id)} · v${environment.revision}</option>`).join("")}</select></label><label class="field">目录权限<select id="workbench-permission"><option value="read-only" ${!workbench.permission_profile || workbench.permission_profile === "read-only" ? "selected" : ""}>只读</option><option value="workspace-write" ${workbench.permission_profile === "workspace-write" ? "selected" : ""}>任务目录可写</option><option value="trusted-local" ${workbench.permission_profile === "trusted-local" ? "selected" : ""} ${state.runtime?.trusted_local_execution ? "" : "disabled"}>可信本机执行${state.runtime?.trusted_local_execution ? "" : "（宿主未启用）"}</option></select></label></div><div class="workbench-tools">${Object.entries(labels).map(([name, label]) => `<label class="toggle-row"><span>${label}</span><input type="checkbox" data-workbench-tool="${name}" ${(workbench.tools || []).includes(name) ? "checked" : ""}></label>`).join("")}</div><small>环境由管理员准备。项目目录默认只读；可信本机命令使用宿主用户权限，需宿主明确允许。</small></section>`;
}
function settings() {
  ensureSettings();
  const d = state.settingsDraft;
  return `<div class="settings-layout"><aside class="settings-list"><h2>Agent 配置</h2>${state.agents
    .filter((a) => a.kind !== "external")
    .map(
      (a) =>
        `<button class="settings-agent ${state.settingsId === a.id ? "active" : ""}" data-action="select-settings" data-id="${escape(a.id)}">${avatar(a.id)}${escape(a.name)}${agentStatusDot(agentStates.forAgent(a.id))}</button>`,
    )
    .join(
      "",
    )}<button class="settings-agent" data-action="add-agent">${icon("plus")}添加 Agent</button><details class="runtime-settings"><summary>执行预算（全局）</summary><label class="field">全局执行槽<input id="global-slots" type="number" min="1" max="256" value="${state.config.max_concurrency || 16}" aria-label="全局执行槽"></label><label class="field">外部预留执行槽<select id="reserved-slots" aria-label="外部预留执行槽">${Array.from({ length: state.config.max_concurrency || 16 }, (_, n) => n).map((n) => `<option value="${n}" ${state.config.reserved_external_slots === n ? "selected" : ""}>${n} 个 · 受管最多 ${(state.config.max_concurrency || 16) - n} 个</option>`).join("")}</select></label><button class="btn small" data-action="save-budget">保存预算</button><p class="form-hint">全局预算限制同时执行的任务数量；外部执行可预留容量。</p></details></aside>
    <form id="agent-form" class="settings-form"><p class="eyebrow">AGENT SETTINGS</p><h1>${escape(state.settingsId ? "配置" + d.name : "添加 Agent")}</h1><p>先连接模型，再补充工作指令与工具。保存后可测试连接。</p><ol class="setup-path"><li>基础与模型</li><li>工作指令</li><li>工具与技能</li></ol>${integrationUI.profileTabs()}${state.settingsTab === "general" ? agentGeneral(d) + workbenchSettings(d) : integrationUI.profilePanel()}<footer class="settings-footer"><div>${d.has_api_key ? '<span class="secret-status">密钥已设置</span> <button type="button" class="text-link" data-action="delete-secret">删除密钥</button>' : '<span class="secret-status">本机配置 · 仓库之外</span>'}</div><button class="btn primary" type="submit" ${state.savingSettings ? "disabled" : ""}>${icon("check")}${state.savingSettings ? "正在保存…" : "保存配置"}</button></footer><div class="storage-note">${icon("shield")}<div>配置、凭据与产物保存在本机。新的执行使用已保存配置。</div></div></form></div>`;
}
function commandResults() {
  const query = (state.modal?.query || "").trim().toLocaleLowerCase();
  const entries = [
    ...state.mcpServers.map((s) => ({
      group: "MCP 服务",
      title: s.name,
      action: "mcp-select",
      id: s.id,
      icon: "plug",
    })),
    {
      group: "页面",
      title: "MCP 集成",
      action: "route",
      route: "mcp",
      icon: "plug",
    },
    {
      group: "快捷操作",
      title: "注册 MCP 服务",
      action: "mcp-new",
      icon: "plug",
    },
    {
      group: "快捷操作",
      title: "创建会话",
      action: "new-session",
      icon: "plus",
    },
    {
      group: "快捷操作",
      title: "工作流画布",
      action: "route",
      route: "workflows",
      icon: "flow",
    },
    {
      group: "快捷操作",
      title: "添加 Agent",
      action: "add-agent",
      icon: "agents",
    },
    ...[
      ["overview", "总览", "grid"],
      ["tasks", "任务", "flow"],
      ["messages", "协作消息", "mail"],
      ["artifacts", "工作产物", "folder"],
    ].map(([mode, title, ico]) => ({
      group: "页面",
      title,
      icon: ico,
      action: "mode",
      mode,
    })),
    {
      group: "页面",
      title: "设置与配置",
      action: "route",
      route: "settings",
      icon: "settings",
    },
    ...state.snapshot.threads.map((t) => ({
      group: "独立对话",
      title: t.title,
      action: "select-thread",
      id: t.id,
      icon: "chat",
    })),
    ...state.agents
      .filter((a) => a.enabled && a.kind !== "external")
      .map((a) => ({
        group: "Agent",
        title: a.name,
        action: "agent-chat",
        id: a.id,
        icon: "agents",
      })),
    ...state.snapshot.tasks.map((t) => ({
      group: "任务",
      title: taskTitle(t),
      action: "task-detail",
      id: t.id,
      icon: "flow",
      hint: statusLabel(t),
    })),
  ]
    .filter(
      (entry) =>
        !query ||
        `${entry.title} ${entry.group} ${entry.id || ""}`
          .toLocaleLowerCase()
          .includes(query),
    )
    .slice(0, 30);
  let group = "";
  return (
    entries
      .map((entry) => {
        const heading =
          group !== entry.group
            ? `<div class="command-group">${entry.group}</div>`
            : "";
        group = entry.group;
        return `${heading}<button class="command-result" data-action="${entry.action}" ${entry.id ? `data-id="${escape(entry.id)}"` : ""} ${entry.mode ? `data-mode="${entry.mode}"` : ""} ${entry.route ? `data-route="${entry.route}"` : ""}>${icon(entry.icon)}<span>${escape(entry.title)}</span><small>${escape(entry.hint || entry.group)}</small>${icon("arrow")}</button>`;
      })
      .join("") ||
    '<div class="command-empty">没有匹配结果，试试 Agent 名称、任务内容或页面名称。</div>'
  );
}

function rememberModalOrigin() {
  const element = document.activeElement;
  if (
    !element ||
    element === document.body ||
    $("#modal-root").contains(element)
  )
    return;
  modalReturnFocus = {
    element,
    key: element.dataset.focus || element.id,
    dataset: { ...element.dataset },
    label: element.getAttribute("aria-label") || element.textContent.trim(),
    start: element.selectionStart,
    end: element.selectionEnd,
  };
}
function returnModalFocus() {
  const origin = modalReturnFocus;
  const candidates = [
    ...document.querySelectorAll("[data-action],[data-focus],[id]"),
  ];
  const target =
    (origin?.element.isConnected && origin.element.getClientRects().length
      ? origin.element
      : null) ||
    (origin?.key &&
      candidates.find(
        (e) =>
          e.getClientRects().length &&
          (e.id === origin.key || e.dataset.focus === origin.key),
      )) ||
    (origin?.dataset.action &&
      candidates.find(
        (e) =>
          e.getClientRects().length &&
          Object.entries(origin.dataset).every(
            ([k, v]) => e.dataset[k] === v,
          ) &&
          (e.getAttribute("aria-label") || e.textContent.trim()) ===
            origin.label,
      )) ||
    $(".top-search");
  target?.focus({ preventScroll: true });
  if (target?.setSelectionRange && origin?.start != null) {
    try {
      target.setSelectionRange(origin.start, origin.end);
    } catch {}
  }
  modalReturnFocus = null;
}
function restoreModalFocus(memory) {
  const root = $("#modal-root");
  const same =
    memory?.key &&
    [...root.querySelectorAll("[id],[data-focus]")].find(
      (e) => e.id === memory.key || e.dataset.focus === memory.key,
    );
  const target =
    same ||
    root.querySelector("input:not([type=checkbox]),textarea,select") ||
    root.querySelector(".modal-content button,button");
  target?.focus({ preventScroll: true });
  if (same?.setSelectionRange && memory.start !== null) {
    try {
      same.setSelectionRange(memory.start, memory.end);
    } catch {}
  }
}
function renderModal() {
  const root = $("#modal-root"),
    m = state.modal;
  const wasOpen = !!root.firstElementChild;
  const openTraceNodes = new Set([...root.querySelectorAll("details[data-trace-node][open]")].map(node => node.dataset.traceNode));
  const memory = focusMemory();
  const modalScroll = root.querySelector(".modal-content")?.scrollTop || 0;
  const sourceScroll = root.querySelector(".artifact-source");
  const sourcePosition = sourceScroll ? [sourceScroll.scrollTop, sourceScroll.scrollLeft] : [0, 0];
  if (!wasOpen && m && !modalReturnFocus) rememberModalOrigin();
  $("#app").inert = !!m;
  if (!m) {
    root.innerHTML = "";
    if (wasOpen) returnModalFocus();
    return;
  }
  if (m.type === "command") {
    root.innerHTML = `<div class="modal-backdrop ${wasOpen ? "continuing" : ""}"><section class="modal command-modal" role="dialog" aria-modal="true" aria-labelledby="command-title"><h2 id="command-title" class="sr-only">搜索与快捷操作</h2><header><label class="command-input-wrap">${icon("search")}<input id="command-input" data-focus="command-input" aria-label="搜索与快捷操作" value="${escape(m.query || "")}" placeholder="搜索对话、Agent、任务，或跳转页面…" autocomplete="off"><kbd>Esc</kbd></label></header><div class="modal-content" id="command-results">${commandResults()}</div><footer><span><kbd>↑</kbd> <kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 打开</span><span><kbd>Esc</kbd> 关闭</span></footer></section></div>`;
    restoreModalFocus(memory);
    $("#command-results").scrollTop = modalScroll;
    return;
  }
  let title = "",
    content = "",
    footer = "",
    wide = false;
  if (m.type === "new-session") {
    title = "创建会话";
    content = `<label class="field">会话名称<input id="session-title" data-focus="session-title" placeholder="描述这次协作的目标" value="${escape(m.title || "")}" maxlength="120"></label><p>会话包含独立的 agent 对话、任务、协作消息和产物。</p>`;
    footer =
      '<button class="btn primary" data-action="create-session">创建会话</button>';
  }
  if (m.type === "open-agent") {
    title = "打开 Agent 对话";
    content = `<div class="agent-grid">${agentCards()}</div><p>已有对话会直接打开，也可以在设置中接入新的执行端。</p>`;
    footer = '<button class="btn" data-action="add-agent">添加 Agent</button>';
  }
  if (m.type === "instances") {
    title = `${agent(m.agentId)?.name || "Agent"} · 选择实例`;
    content = `<div class="instance-picker">${m.items.map(item => `<button class="instance-option ${instanceInUse(item) ? "" : "is-idle"}" data-action="select-thread" data-id="${escape(item.thread_id)}" data-focus="instance-${escape(item.thread_id)}">
      <div class="instance-option-heading"><strong>${escape(item.goal || item.title)}</strong>${agentStatusBadge(agentStates.forThread(item))}${instanceUnread(item.unread_count)}</div>
      <span class="instance-origin">${item.workflow_name ? `${escape(item.workflow_name)} · r${item.workflow_revision}` : "独立对话"}${item.delivery_state ? ` · 交付${({ PASSED: "已检查", FAILED: "有缺项", NOT_REQUIRED: "未要求检查" }[item.delivery_state] || "待检查")}` : ""}</span>
      <span class="instance-preview">${markdown(item.summary_markdown || "尚无消息")}</span></button>`).join("")}</div>
      ${m.loading ? '<p class="form-hint">正在读取实例…</p>' : m.has_more ? '<button class="btn small" data-action="more-instances">更多实例</button>' : ""}`;
    footer = `<button class="btn" data-action="new-agent-thread" data-id="${escape(m.agentId)}">${icon("plus")}新建独立对话</button>`;
  }
  if (m.type === "project") {
    title = m.id ? "项目设置" : "创建项目";
    content = `<label class="field">项目名称<input id="project-name" data-focus="project-name" maxlength="120"
      value="${escape(m.name || "")}" placeholder="例如：个人研究"></label>
      <label class="field">本地文件夹<input id="project-folder" data-focus="project-folder"
      value="${escape(m.local_folder || "")}" placeholder="C:\\Projects\\my-project"></label>
      <p>会话和 Agent 按项目独立管理，切换项目会切换整个团队。</p>`;
    footer = `${m.id ? '<button class="btn" data-action="archive-project">归档项目</button><button class="btn danger" data-action="delete-project">删除项目</button>' : ""}
      <button class="btn primary" data-action="save-project">保存项目</button>`;
  }
  if (m.type === "confirm-delete") {
    const project = m.kind === "project";
    title = project ? "删除项目？" : "删除会话？";
    content = `<p>确认删除<strong>“${escape(m.name)}”</strong>？</p>
      <p>${project ? "项目及其会话将移入回收站，项目中的 Agent 将暂不可用。"
        : "会话中的对话、任务记录和产物将随会话移入回收站，项目和 Agent 配置不受影响。"}</p>
      <p>之后可在“回收站”恢复。不会删除本地文件夹或磁盘文件。</p>
      ${project && m.folder ? `<p class="form-hint">本地文件夹保留：${escape(m.folder)}</p>` : ""}`;
    footer = `<button class="btn" data-action="close-modal" ${m.pending ? "disabled" : ""}>取消</button>
      <button class="btn danger" data-action="confirm-delete" ${m.pending ? "disabled" : ""}>${m.pending ? "正在删除…" : project ? "确认删除项目" : "确认删除会话"}</button>`;
  }
  if (m.type === "trash") {
    title = "回收站";
    const projects = m.data?.projects || [], sessions = m.data?.sessions?.items || [];
    content = m.loading ? '<p>正在读取…</p>' :
      `<p class="form-hint">恢复项目时会同时恢复随项目删除的会话。本地文件始终保留。</p>
      ${projects.length ? "<h3>项目</h3>" : ""}
      ${projects.map(p => `<div class="trash-row"><div><strong>${escape(p.name)}</strong><small>${escape(p.local_folder)}</small></div>
        <button class="btn" data-action="restore-deleted" data-kind="project" data-id="${escape(p.id)}" aria-label="恢复项目 ${escape(p.name)}">恢复</button></div>`).join("")}
      ${sessions.length ? "<h3>会话</h3>" : ""}
      ${sessions.map(s => `<div class="trash-row"><div><strong>${escape(s.title)}</strong></div>
        <button class="btn" data-action="restore-deleted" data-kind="session" data-id="${escape(s.id)}" aria-label="恢复会话 ${escape(s.title)}">恢复</button></div>`).join("")}
      ${!projects.length && !sessions.length ? "<p>回收站为空。</p>" : ""}
      ${m.data?.sessions?.has_more ? '<button class="text-link" data-action="trash-more">加载更多会话</button>' : ""}`;
    footer = '<button class="btn" data-action="close-modal">关闭</button>';
  }
  if (m.type === "message") {
    title = "发送协作消息";
    content = `<div class="form-grid"><label class="field full">目标对话<select id="message-target" data-focus="message-target">${state.snapshot.threads.map((t) => `<option value="${escape(t.id)}" ${m.target === t.id ? "selected" : ""}>${escape(t.title)}</option>`).join("")}</select></label><label class="field full">消息内容<textarea id="message-content" data-focus="message-content" placeholder="发送目标 agent 需要的信息…">${escape(m.content || "")}</textarea></label></div><p>消息会先保存为待应用。目标正在执行时，下一次调用会读取；空闲时可以显式启动接收任务。</p>`;
    footer =
      '<button class="btn primary" data-action="send-message">发送消息</button>';
  }
  if (m.type === "workflow") {
    title = "安排协作任务";
    wide = true;
    content = `<p>为已有对话安排任务，并选择依赖。每个节点有自己的接收与完成状态。</p>${state.workflowRows.map((r, i) => `<div class="workflow-row"><h3>任务 ${i + 1}<button class="icon-btn" data-action="remove-workflow-row" data-index="${i}" aria-label="删除任务 ${i + 1}">${icon("close")}</button></h3><div class="form-grid"><label class="field">Agent 对话<select data-workflow="thread_id" data-index="${i}" data-focus="workflow-thread-${i}">${state.snapshot.threads.map((t) => `<option value="${escape(t.id)}" ${r.thread_id === t.id ? "selected" : ""}>${escape(t.title)}</option>`).join("")}</select></label><label class="field">等待哪个任务<select data-workflow="dependency" data-index="${i}" data-focus="workflow-dependency-${i}"><option value="">独立运行</option>${state.workflowRows.map((_, j) => (j === i ? "" : `<option value="${j}" ${String(r.dependency) === String(j) ? "selected" : ""}>任务 ${j + 1}</option>`)).join("")}</select></label><label class="field full">指令<textarea data-workflow="input" data-index="${i}" data-focus="workflow-input-${i}">${escape(r.input || "")}</textarea></label></div></div>`).join("")}<button class="btn" data-action="add-workflow-row">${icon("plus")}添加任务</button>`;
    footer =
      '<button class="btn primary" data-action="submit-workflow">提交任务</button>';
  }
  if (m.type === "task") {
    title = "任务详情";
    wide = true;
    content = taskDetail(m.task);
    footer = `${m.task ? `<button class="btn" data-action="select-thread" data-id="${escape(m.task.thread_id)}">打开对话</button>` : ""}<button class="btn" data-action="close-modal">关闭</button>`;
  }
  if (m.type === "artifact") {
    title = escape(m.artifact.name);
    wide = true;
    const preview = m.preview;
    content = `<div class="artifact-preview-meta"><span>只读原文</span><span>${escape(artifactSourceLabel(m.artifact))}</span><span>${formatBytes(m.artifact.size)}</span>${preview?.encoding ? `<span>${escape(preview.encoding.toUpperCase())}</span>` : ""}</div>${m.loading ? '<p class="loading-inline" role="status">正在读取产物原文…</p>' : m.error ? `<p class="artifact-preview-error" role="alert">${escape(m.error)}</p>` : preview?.kind === "text" ? `${preview.truncated ? '<p class="artifact-preview-notice">文件较大，仅显示前 1 MB；下载可获取完整文件。</p>' : ""}${preview.text ? `<pre class="artifact-source" data-focus="artifact-source" tabindex="0" role="region" aria-label="产物原文"><code></code></pre>` : '<p class="quiet-state">这是一个空文本文件。</p>'}` : `<p class="quiet-state">${escape(preview?.message || "此格式暂不支持原文预览。")}</p>`}`;
    footer = `${preview?.kind === "text" ? `<button class="btn" data-action="copy-artifact-text">${preview.truncated ? "复制已显示文本" : "复制原文"}</button>` : ""}<a class="btn" href="${artifactDownload(m.artifact.id)}">${icon("download")}下载文件</a><button class="btn primary" data-action="close-modal">关闭</button>`;
  }
  if (m.type === "codex") {
    title = "连接外部 Codex";
    content = `<div class="codex-guide"><h3>${icon("terminal")}对话留在 Codex，协作通过接口进行</h3><p>平台不接管 Codex 的模型、密钥和聊天。当前为外部执行预留 <strong>${state.config.reserved_external_slots || 0}</strong> 个槽。</p></div><p><strong>CLI</strong><br><code>oea request METHOD --params-file PATH</code></p><p><strong>MCP · 连接已启动的平台</strong><br><code>oea mcp --connect --compact</code></p><p><strong>HTTP</strong><br>协调端使用本机凭据调用 <code>/api/rpc</code>，可提交子任务、查询结果、发送协作消息和获取产物引用。</p><p class="form-hint">完整参数与生命周期约定见仓库的接口说明。参数文件、凭据和输出存放在源码目录之外。</p>`;
    footer =
      '<button class="btn primary" data-action="mcp-sdk">SDK 与共享 MCP 接入</button><button class="btn" data-action="close-modal">知道了</button>';
  }
  const extensionModal = integrationUI.modal(m);
  if (extensionModal) {
    title = extensionModal.title;
    content = extensionModal.content;
    footer = extensionModal.footer;
    wide = !!extensionModal.wide;
  }
  root.innerHTML = `<div class="modal-backdrop ${wasOpen ? "continuing" : ""}"><section class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><header><h2 id="dialog-title">${title}</h2><button class="icon-btn" data-action="close-modal" aria-label="关闭对话框" ${m.pending ? "disabled" : ""}>${icon("close")}</button></header><div class="modal-content">${content}</div><footer>${footer}</footer></section></div>`;
  for (const node of root.querySelectorAll("details[data-trace-node]")) node.open = openTraceNodes.has(node.dataset.traceNode);
  if (m.type === "artifact") {
    const source = root.querySelector(".artifact-source code");
    if (source) {
      source.textContent = m.preview.text;
      source.parentElement.scrollTop = sourcePosition[0];
      source.parentElement.scrollLeft = sourcePosition[1];
    }
  }
  restoreModalFocus(memory);
  root.querySelector(".modal-content").scrollTop = modalScroll;
}
function taskDetail(task) {
  if (!task) return '<div class="loading-inline">正在读取任务…</div>';
  const partial = task.result?.metadata?.acceptance?.state === "FAILED";
  const modal = state.modal;
  return `<div class="task-detail"><div class="split">${badge(task)}<span class="task-id">${escape(task.id)}</span></div><h3>指令</h3>${content(task.spec?.payload?.input || task.title || "协作任务")}${task.queue_reason ? `<p>等待原因：${escape(task.queue_reason === "MODEL_CONFIGURATION" ? "等待模型配置（保存后自动继续）" : task.queue_reason)}</p>` : ""}${task.error ? `<h3>${executionState(task) === "SUCCEEDED" ? "结果反馈" : "状态说明"}</h3><div class="message-text">${markdown(task.error.safe_message || task.error.code || "执行异常")}</div>` : ""}${task.result ? `<h3>${partial ? "部分结果" : "交付结果"}</h3><div class="message-text">${markdown(task.result.summary_markdown || (partial ? "未取得有效正文，可查看执行过程与已保存产物。" : "结果已保存，完整内容可供协作读取。"))}</div>` : ""}
    ${modal?.trace ? executionTrace(modal.trace, modal.receipts, modal.receiptLoading) : ""}${modal?.traceError ? `<p class="execution-error" role="alert">${escape(modal.traceError)}</p>` : ""}${!modal?.trace || modal.trace.has_more ? `<button class="text-link" data-action="task-trace" ${modal?.traceLoading ? "disabled" : ""}>${modal?.traceLoading ? "正在读取…" : modal?.trace ? "加载更多执行记录" : "查看执行过程"}</button>` : ""}
    <h3>执行尝试</h3>${(task.runs || []).map((r) => `<div class="kv"><span>Run ${r.attempt_no} · ${time(r.started_at)}</span><strong>${escape(statusLabel(r))}</strong></div>`).join("") || "<p>尚未派发。</p>"}
    ${task.spec?.depends_on?.length ? `<h3>依赖任务</h3>${task.spec.depends_on.map((id) => `<button class="text-link" data-action="task-detail" data-id="${escape(id)}">${escape(id)}</button>`).join("<br>")}` : ""}
    <div class="inline-actions">${canContinue(task, thread(task.thread_id)) ? `<button class="btn primary" data-action="continue-task" data-id="${escape(task.id)}">继续完善</button>` : ""}${["RUNNING", "QUEUED", "WAITING"].includes(task.state) ? `<button class="btn danger" data-action="cancel-task" data-id="${escape(task.id)}">提交取消申请</button>` : ""}${terminal.has(task.state) ? `<button class="btn" data-action="retry-task" data-id="${escape(task.id)}">创建重试任务</button>` : ""}</div>
    ${task.state === "WAITING" && task.wait?.kind === "input" ? `<h3>提供等待输入</h3><label class="field"><textarea id="resume-content" data-focus="resume-content" aria-label="恢复任务的补充信息" placeholder="为当前任务提供输入">${escape(state.modal?.resume || "")}</textarea></label><div class="toolbar"><button class="btn primary" data-action="resume-task" data-id="${escape(task.id)}">恢复任务</button></div>` : ""}
    ${
      task.state === "UNCERTAIN"
        ? `<h3>核对并处理</h3><p>确认旧执行已停止，并核对外部动作后，保存处理依据。</p><div class="form-grid"><label class="field full">核对依据<textarea id="reconcile-evidence" data-focus="reconcile-evidence" placeholder="说明执行停止与外部动作的核对依据">${escape(state.modal?.evidence || "")}</textarea></label><label class="field">处理结果<select id="reconcile-resolution">${[
            ["FAILED", "确认失败"],
            ["CANCELLED", "确认取消"],
            ["QUEUED", "确认可安全重试"],
          ]
            .map(
              ([value, label]) =>
                `<option value="${value}" ${(state.modal?.resolution || "FAILED") === value ? "selected" : ""}>${label}</option>`,
            )
            .join(
              "",
            )}</select></label></div><label class="toggle-row"><span>已确认执行停止，外部动作已核对</span><input id="reconcile-confirmed" type="checkbox" ${state.modal?.confirmed ? "checked" : ""}></label><button class="btn primary" data-action="reconcile-task" data-id="${escape(task.id)}">保存核对处理</button>`
        : ""
    }</div>`;
}

async function loadBootstrap(projectId = state.project?.id || localStorage.getItem("ma-project")) {
  const value = await api("/bootstrap" + (projectId ? "?project_id=" + encodeURIComponent(projectId) : ""));
  Object.assign(state, {
    projects: value.projects || [],
    project: value.project,
    agents: value.agents,
    environments: value.environments || [],
    mcpServers: value.mcp_servers || [],
    skillRegistry: value.skill_registry || { items: [] },
    sessions: value.sessions,
    config: value.config,
    runtime: value.runtime,
  });
  if (state.project) localStorage.setItem("ma-project", state.project.id);
}
async function selectProject(id) {
  if (state.project?.id === id) return;
  const workflowRoute = state.route === "workflows";
  eventSource?.close();
  state.sessionEpoch++;
  state.syncGeneration++;
  state.session = null;
  state.selectedThread = null;
  state.openTabs = [];
  state.conversations.clear();
  state.activities.clear();
  state.streams.clear();
  state.drafts.clear();
  state.turnRequests.clear();
  state.settingsId = null;
  state.settingsDraft = null;
  state.modal = null;
  state.route = workflowRoute ? "workflows" : "workspace";
  state.mode = "overview";
  await loadBootstrap(id);
  if (workflowRoute) await workflowUI.load(state.project?.id);
  await selectSession(state.sessions.find(s => s.state === "OPEN")?.id);
}
async function refreshAfterDeletion(kind, id) {
  if (kind === "project" && state.project?.id === id) {
    localStorage.removeItem("ma-project");
    state.project = null;
    await selectProject("");
  } else if (kind === "session" && state.session?.id === id) {
    for (const t of state.snapshot.threads) {
      state.conversations.delete(t.id);
      state.drafts.delete(t.id);
      state.turnRequests.delete(t.id);
    }
    localStorage.removeItem("ma-tabs-" + id);
    await loadBootstrap();
    await selectSession((state.sessions.find(s => s.state === "OPEN") || state.sessions[0])?.id);
  } else {
    await loadBootstrap();
    render();
  }
}
function rememberTabs() {
  if (state.session)
    localStorage.setItem(
      "ma-tabs-" + state.session.id,
      JSON.stringify(state.openTabs),
    );
}
async function selectSession(id) {
  eventSource?.close();
  eventSource = null;
  state.sessionEpoch++;
  state.syncGeneration++;
  state.modal = null;
  state.connection = "connecting";
  state.selectedThread = null;
  state.artifactQuery = "";
  state.messageFilter = "all";
  state.resources = [];
  state.session = state.sessions.find((s) => s.id === id) || null;
  state.streams.clear();
  state.events = [];
  state.snapshot = { threads: [], tasks: [], messages: [], artifacts: [] };
  if (!state.session) {
    state.openTabs = [];
    localStorage.removeItem("ma-session");
    connectEvents(state.sessionEpoch);
    render();
    return;
  }
  localStorage.setItem("ma-session", id);
  try {
    state.openTabs = JSON.parse(localStorage.getItem("ma-tabs-" + id) || "[]");
  } catch {
    state.openTabs = [];
  }
  const epoch = state.sessionEpoch,
    snapshot = await api(`/sessions/${id}/snapshot`);
  if (epoch !== state.sessionEpoch) return;
  state.snapshot = snapshot;
  for (const live of snapshot.progress || []) state.streams.set(live.run_id, {thread_id: live.thread_id, text: live.summary_markdown, tool: ""});
  state.session = snapshot.session;
  state.cursor = snapshot.cursor;
  state.openTabs = state.openTabs.filter((id) => thread(id));
  connectEvents(epoch);
  render();
}
function connectEvents(epoch) {
  eventSource?.close();
  const source = new EventSource(
    state.session ? `/api/sessions/${encodeURIComponent(state.session.id)}/events?after_seq=${state.cursor}` : "/api/presence",
  );
  eventSource = source;
  eventSource.onopen = async () => {
    if (epoch !== state.sessionEpoch || source !== eventSource) return;
    if (state.connection === "offline") {
      state.connection = "connecting";
      render();
      try {
        if (state.session) await refreshSession();
        else await loadBootstrap();
      } catch (error) {
        if (epoch !== state.sessionEpoch || source !== eventSource) return;
        state.connection = "offline";
        render();
        toast(error.message, true);
        return;
      }
      if (epoch !== state.sessionEpoch || source !== eventSource) return;
    }
    state.connection = "online";
    render();
  };
  eventSource.onerror = () => {
    if (epoch !== state.sessionEpoch || source !== eventSource) return;
    state.connection = "offline";
    render();
  };
  eventSource.addEventListener("runtime", (event) => {
    if (epoch !== state.sessionEpoch || source !== eventSource) return;
    const value = JSON.parse(event.data);
    if (value.type === "stream.resync") {
      resync().catch(() => {});
      return;
    }
    if (value.durable) {
      if (value.seq <= state.cursor) return;
      state.cursor = value.seq;
      if (value.type === "run.stage") {
        const modal = state.modal;
        if (modal?.type === "task" && modal.task?.id === value.task_id && modal.trace) {
          modal.trace.items = [...modal.trace.items, { ...value.payload, seq: value.seq, run_id: value.run_id }].slice(-200);
          renderModal();
        }
        return;
      }
      state.events.push(value);
      if (["mcp.capacity_reconciled", "mcp.capacity_isolated", "mcp.capacity_observed"].includes(value.type) &&
          state.modal?.task?.id === value.task_id && state.modal.trace?.resource_calls) {
        const calls = state.modal.trace.resource_calls;
        state.modal.trace.resource_calls = calls.some(call => call.call_id === value.payload.call_id)
          ? calls.map(call => call.call_id === value.payload.call_id ? { ...call, ...value.payload } : call)
          : [...calls, { ...value.payload, run_id: value.run_id }].slice(-100);
        renderModal();
      }
      state.events = state.events.slice(-100);
      workflowUI.onEvent(value).catch((error) => toast(error.message, true));
      if (value.type === "session.deleted" && value.payload?.cascade) return;
      if (!state.modal?.pending && value.type === "session.deleted" && value.session_id === state.session?.id) {
        refreshAfterDeletion("session", value.session_id).catch(error => toast(error.message, true));
        return;
      }
      if (!state.modal?.pending && value.type === "project.deleted" && value.payload?.project_id === state.project?.id) {
        refreshAfterDeletion("project", value.payload.project_id).catch(error => toast(error.message, true));
        return;
      }
      scheduleRefresh();
    } else if (value.type === "run.progress" || value.type === "run.log") {
      const currentTask = state.snapshot.tasks.find(t => t.id === value.task_id);
      if (currentTask && terminal.has(currentTask.state)) return;
      const existing = state.streams.get(value.run_id) || {
        thread_id: value.thread_id,
        text: "",
        tool: "",
      };
      if (value.payload.delta) { existing.text += value.payload.delta; existing.tool = ""; }
      if (value.payload.tool) existing.tool = "调用 " + value.payload.tool;
      state.streams.set(value.run_id, existing);
      const live = $("#live-messages"),
        t = thread(state.selectedThread);
      if (live && t && value.thread_id === t.id) {
        const c = $(".conversation"),
          bottom = c.scrollHeight - c.scrollTop - c.clientHeight < 100;
        live.innerHTML = liveMessages(t);
        if (bottom) c.scrollTop = c.scrollHeight;
      }
    }
  });
}
async function resync() {
  if (!state.session) return;
  state.connection = "connecting";
  render();
  const epoch = state.sessionEpoch,
    generation = ++state.syncGeneration;
  eventSource?.close();
  eventSource = null;
  state.streams.clear();
  const snapshot = await api(`/sessions/${state.session.id}/snapshot`);
  if (epoch !== state.sessionEpoch || generation !== state.syncGeneration)
    return;
  state.snapshot = snapshot;
  state.cursor = snapshot.cursor;
  connectEvents(state.sessionEpoch);
  await refreshConversation(state.selectedThread);
  await workflowUI.refresh();
  render();
}
async function reconnect() {
  state.connection = "connecting";
  render();
  try {
    await loadBootstrap();
    if (state.session) await resync();
    else {
      connectEvents(state.sessionEpoch);
      render();
    }
  } catch (error) {
    state.connection = "offline";
    render();
    throw error;
  }
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshSession().catch(() => {}), 100);
}
async function refreshSession() {
  if (!state.session) return;
  const epoch = state.sessionEpoch,
    generation = state.syncGeneration,
    [snapshot, bootstrap] = await Promise.all([
      api(`/sessions/${state.session.id}/snapshot`),
      api("/bootstrap?project_id=" + encodeURIComponent(state.project.id)),
    ]);
  if (
    epoch !== state.sessionEpoch ||
    generation !== state.syncGeneration ||
    snapshot.cursor < (state.snapshot.cursor || 0)
  )
    return;
  state.snapshot = snapshot;
  for (const [id, request] of state.continuationRequests) {
    if ((snapshot.tasks.find(task => task.id === id)?.continuation_runs ?? 0) > request.runs) state.continuationRequests.delete(id);
  }
  state.runtime = bootstrap.runtime;
  state.projects = bootstrap.projects || [];
  state.agents = bootstrap.agents;
  state.sessions = bootstrap.sessions;
  state.mcpServers = bootstrap.mcp_servers || [];
  state.skillRegistry = bootstrap.skill_registry || { items: [] };
  state.session = snapshot.session;
  await refreshConversation(state.selectedThread);
  for (const [id] of state.streams) {
    const t = snapshot.tasks.find((t) => t.runs?.some((r) => r.id === id));
    const runTask = snapshot.tasks.find(
      (t) => t.id === state.events.find((e) => e.run_id === id)?.task_id,
    );
    if ((t || runTask) && !["RUNNING", "QUEUED"].includes((t || runTask).state))
      state.streams.delete(id);
  }
  if (state.modal?.type === "task" && state.modal.task) {
    const modal = state.modal;
    const task = await api(`/tasks/${modal.task.id}`);
    if (state.modal === modal) modal.task = task;
  }
  render();
}
async function refreshConversation(id, older = false) {
  if (!id) return;
  const epoch = state.sessionEpoch, current = state.conversations.get(id), activity = state.activities.get(id);
  const query = older && current?.cursor ? `&before_seq=${current.cursor}` : "";
  const [result, feed] = await Promise.all([
    api(`/threads/${id}/conversation?latest=true&limit=100${query}`),
    rpc("threads.activity", { thread_id: id, limit: 100, ...(older && activity?.cursor ? { cursor: activity.cursor } : {}) }),
  ]);
  if (epoch !== state.sessionEpoch) return;
  state.conversations.set(id, mergeHistoryPage(current, result, older));
  state.activities.set(id, mergeHistoryPage(activity, feed, older, "position"));
}
async function selectThread(id) {
  if (!thread(id)) return;
  state.modal = null;
  state.route = "workspace";
  state.selectedThread = id;
  if (!state.openTabs.includes(id)) {
    state.openTabs.push(id);
    rememberTabs();
  }
  render();
  await refreshConversation(id);
  render();
  if ($(".conversation"))
    $(".conversation").scrollTop = $(".conversation").scrollHeight;
}
async function openAgent(id, forceNew = false) {
  if (!state.session) {
    state.modal = { type: "new-session", pendingAgent: id };
    renderModal();
    return;
  }
  const epoch = state.sessionEpoch;
  const existing = state.snapshot.threads.filter(t => t.agent_id === id);
  if (!forceNew && existing.length === 1) return selectThread(existing[0].id);
  if (!forceNew && existing.length > 1) {
    const modal = { type: "instances", agentId: id, items: groups().find(group => group.agent.id === id)?.instances || [], loading: true };
    state.modal = modal;
    renderModal();
    try {
      const page = await rpc("threads.instances", { session_id: state.session.id, agent_id: id });
      if (state.modal === modal) Object.assign(modal, page);
    } finally {
      modal.loading = false;
      if (state.modal === modal) renderModal();
    }
    return;
  }
  const count = state.snapshot.threads.filter((t) => t.agent_id === id).length;
  const value = await api("/threads", {
    method: "POST",
    body: {
      session_id: state.session.id,
      agent_id: id,
      title: (agent(id)?.name || id) + (count ? ` · ${count + 1}` : ""),
    },
  });
  if (epoch !== state.sessionEpoch) return;
  state.snapshot.threads.push(value);
  return selectThread(value.id);
}
async function sendTurn(id, content) {
  if (state.busy) return;
  state.busy = true;
  let request = state.turnRequests.get(id);
  if (!request || request.content !== content) {
    request = { content, key: idempotency() };
    state.turnRequests.set(id, request);
  }
  try {
    await api(`/threads/${id}/turns`, {
      method: "POST",
      body: { content, idempotency_key: request.key },
    });
    state.turnRequests.delete(id);
    if ((state.drafts.get(id) || "").trim() === content)
      state.drafts.set(id, "");
    toast("指令已保存，等待调度");
    await refreshSession();
  } finally {
    state.busy = false;
    render();
  }
}
async function upload() {
  if (!state.session) {
    toast("先创建一个会话");
    return;
  }
  const sessionId = state.session.id,
    targetThread = state.selectedThread;
  const input = document.createElement("input");
  input.type = "file";
  input.onchange = async () => {
    try {
      const file = input.files[0];
      if (!file) return;
      const value = await api(`/artifacts?session_id=${sessionId}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Filename": encodeURIComponent(file.name),
        },
        body: file,
      });
      toast("产物已收存");
      if (targetThread) {
        const before = state.drafts.get(targetThread) || "";
        state.drafts.set(
          targetThread,
          before + `\nArtifactRef: ${value.id} (${value.name})`,
        );
      }
      await refreshSession();
    } catch (e) {
      toast(e.message, true);
    }
  };
  input.click();
}
async function action(name, element) {
  if (agentLaunch.action(name, element)) return;
  if (name.startsWith("wf-")) return workflowUI.action(name, element);
  const id = element.dataset.id;
  if (name === "artifact-preview") {
    const artifact = state.snapshot.artifacts.find(item => item.id === id);
    if (!artifact) throw new Error("当前会话中未找到此产物。");
    const modal = { type: "artifact", artifact, loading: true };
    state.modal = modal;
    renderModal();
    try { modal.preview = await readArtifactPreview(artifact); }
    catch (error) { modal.error = error.message; }
    if (state.modal === modal) { modal.loading = false; renderModal(); }
    return;
  }
  if (name === "copy-artifact-text") {
    if (state.modal?.type !== "artifact" || state.modal.preview?.kind !== "text") return;
    await navigator.clipboard.writeText(state.modal.preview.text);
    toast("已复制产物原文");
    return;
  }
  if (name === "delete-project" || name === "delete-session") {
    const project = name === "delete-project", target = project ? state.project : state.session;
    if (!target) return;
    state.modal = {type: "confirm-delete", kind: project ? "project" : "session",
      id: target.id, name: project ? target.name : target.title, folder: project ? target.local_folder : ""};
    renderModal();
    return;
  }
  if (name === "confirm-delete") {
    const m = state.modal;
    if (m?.type !== "confirm-delete" || m.pending) return;
    m.pending = true;
    renderModal();
    try {
      await api(`/${m.kind === "project" ? "projects" : "sessions"}/${encodeURIComponent(m.id)}`,
        {method: "DELETE", body: {confirmed: true}});
      state.modal = null;
      await refreshAfterDeletion(m.kind, m.id);
      toast("已移入回收站，可恢复");
    } catch (error) {
      if (state.modal === m) { m.pending = false; renderModal(); }
      throw error;
    }
    return;
  }
  if (name === "open-trash") {
    const modal = {type: "trash", loading: true};
    state.modal = modal;
    renderModal();
    const data = await api("/trash");
    if (state.modal === modal) { modal.data = data; modal.loading = false; renderModal(); }
    return;
  }
  if (name === "trash-more") {
    const m = state.modal;
    const data = await api("/trash?cursor=" + encodeURIComponent(m.data.sessions.cursor || ""));
    if (state.modal === m) {
      m.data = {projects: data.projects, sessions: {...data.sessions, items: [...m.data.sessions.items, ...data.sessions.items]}};
      renderModal();
    }
    return;
  }
  if (name === "restore-deleted") {
    const m = state.modal, kind = element.dataset.kind;
    await api(`/${kind === "project" ? "projects" : "sessions"}/${encodeURIComponent(id)}/restore`, {method: "POST", body: {}});
    await loadBootstrap();
    const data = await api("/trash");
    if (state.modal === m) { m.data = data; render(); }
    toast("已恢复");
    return;
  }
  if (name === "new-project" || name === "edit-project") {
    state.modal = {type: "project", key: idempotency(),
      ...(name === "edit-project" && state.project ? {id: state.project.id, name: state.project.name, local_folder: state.project.local_folder} : {})};
    renderModal();
    return;
  }
  if (name === "save-project") {
    const m = state.modal;
    const body = {name: $("#project-name").value.trim(), local_folder: $("#project-folder").value.trim()};
    if (!m.id) body.idempotency_key = m.key;
    const p = await api(m.id ? `/projects/${m.id}` : "/projects", {method: m.id ? "PATCH" : "POST", body});
    state.modal = null;
    if (p.id === state.project?.id) { await loadBootstrap(p.id); render(); }
    else await selectProject(p.id);
    toast("项目已保存");
    return;
  }
  if (name === "archive-project") {
    await api(`/projects/${state.modal.id}/archive`, {method: "POST", body: {archived: true}});
    state.project = null;
    await selectProject("");
    toast("项目已归档，历史已保留");
    return;
  }
  if (await integrationUI.action(name, element)) return;
  if (name === "toggle-inspector") {
    state.inspectorOpen = !state.inspectorOpen;
    render();
    (state.inspectorOpen
      ? $(".inspector-close")
      : $("[data-action=toggle-inspector]")
    )?.focus();
  }
  if (name === "command") {
    state.modal = { type: "command", query: "" };
    render();
    $("#command-input")?.focus();
  }
  if (name === "message-filter") {
    state.messageFilter = element.dataset.filter;
    render();
  }
  if (name === "reconnect") {
    await reconnect();
  }
  if (name === "route") {
    state.route = element.dataset.route;
    state.modal = null;
    render();
    $(".page-body").scrollTop = 0;
    if (["mcp", "skills"].includes(state.route)) {
      await loadBootstrap();
      render();
    }
    if (state.route === "workflows") await workflowUI.load(state.project?.id, true);
  }
  if (name === "mode") {
    state.route = "workspace";
    state.mode = element.dataset.mode;
    state.selectedThread = null;
    state.modal = null;
    render();
    $(".page-body").scrollTop = 0;
    if (state.mode === "tasks") await action("resources", element);
  }
  if (name === "new-session") {
    state.modal = state.project ? { type: "new-session" } : {type: "project", key: idempotency()};
    renderModal();
    $("#session-title")?.focus();
  }
  if (name === "create-session") {
    const title = $("#session-title").value.trim();
    if (!title) {
      toast("请输入会话名称");
      return;
    }
    const pending = state.modal.pendingAgent;
    const value = await api("/sessions", { method: "POST", body: { title, project_id: state.project?.id } });
    state.sessions.push(value);
    state.modal = null;
    await selectSession(value.id);
    if (pending) await openAgent(pending);
  }
  if (name === "open-agent") {
    if (!state.session) {
      state.modal = { type: "new-session" };
    } else {
      state.modal = { type: "open-agent" };
    }
    renderModal();
  }
  if (name === "agent-chat") await openAgent(id);
  if (name === "new-agent-thread") await openAgent(id, true);
  if (name === "more-instances" && state.modal?.type === "instances") {
    const modal = state.modal;
    if (modal.loading) return;
    modal.loading = true;
    renderModal();
    try {
      const page = await rpc("threads.instances", { session_id: state.session.id, agent_id: modal.agentId, cursor: modal.cursor });
      if (state.modal === modal) Object.assign(modal, { ...page, items: [...modal.items, ...page.items] });
    } finally { modal.loading = false; if (state.modal === modal) renderModal(); }
  }
  if (name === "select-thread") await selectThread(id);
  if (name === "close-tab") {
    state.openTabs = state.openTabs.filter((t) => t !== id);
    if (state.selectedThread === id) state.selectedThread = null;
    rememberTabs();
    render();
  }
  if (name === "close-modal") {
    if (state.modal?.pending) return;
    state.modal = null;
    renderModal();
    requestAnimationFrame(observeVisibleActivity);
  }
  if (name === "codex") {
    state.modal = { type: "codex" };
    renderModal();
  }
  if (name === "add-agent") {
    state.settingsTab = "general";
    state.selectedSkill = 0;
    state.profilePreview = true;
    state.settingsId = null;
    state.settingsDraft = {
      id: "",
      name: "",
      project_id: state.project?.id,
      kind: "llm",
      enabled: true,
      llm_required: true,
      base_url: "",
      model: "",
      api_key: "",
      system_prompt: "",
      temperature: null,
      reasoning_effort: null,
      max_output_tokens: 32768,
      token_parameter: "max_completion_tokens",
      max_concurrency: 4,
      timeout_seconds: 120,
      collaboration: true,
      agent_md: "",
      skills: [],
      mcp_bindings: [],
    };
    state.route = "settings";
    state.modal = null;
    render();
  }
  if (name === "select-settings") {
    state.selectedSkill = 0;
    state.profilePreview = true;
    state.settingsId = id;
    state.settingsDraft = { ...structuredClone(agent(id)), api_key: "" };
    render();
  }
  if (name === "test-model") {
    const d = state.settingsDraft, saved = agent(state.settingsId);
    if (!saved || state.testingModelId || state.savingSettings) return;
    const fields = ["kind", "llm_required", "base_url", "model", "temperature", "reasoning_effort", "max_output_tokens", "context_window_tokens", "context_budget_tokens", "token_parameter", "timeout_seconds"];
    if (d.api_key || fields.some((key) => (d[key] ?? null) !== (saved[key] ?? null))) {
      toast("请先保存模型配置，再测试连接。", true);
      return;
    }
    state.testingModelId = saved.id;
    state.modelTestResult = null;
    const stillCurrent = () => state.settingsId === saved.id && !state.settingsDraft.api_key && fields.every((key) => (state.settingsDraft[key] ?? null) === (saved[key] ?? null));
    render();
    try {
      const result = await api(`/agents/${encodeURIComponent(saved.id)}/model-test`, {method: "POST", body: {}});
      if (stillCurrent()) state.modelTestResult = {id: saved.id, ok: true, message: `连接成功 · ${result.model} · ${result.latency_ms} ms。业务效果请另行验收。`};
    } catch (error) {
      if (stillCurrent()) state.modelTestResult = {id: saved.id, ok: false, message: `连接未通过：${error.message}${error.details?.provider_message ? " · " + error.details.provider_message : ""}`};
    } finally {
      state.testingModelId = null;
      render();
    }
  }
  if (name === "delete-secret") {
    await api(`/agents/${state.settingsId}/secret`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    });
    state.settingsDraft.has_api_key = false;
    await loadBootstrap();
    render();
    toast("密钥已删除");
  }
  if (name === "save-budget") {
    const reserved = Number($("#reserved-slots").value);
    state.config = await api("/config", {
      method: "PUT",
      body: { ...state.config, max_concurrency: Number($("#global-slots").value), reserved_external_slots: reserved },
    });
    toast("执行预算已保存");
    render();
  }
  if (name === "compose-message") {
    if (!state.snapshot.threads.length) {
      toast("先打开一个 Agent 对话");
      return;
    }
    state.modal = {
      type: "message",
      key: idempotency(),
      target:
        state.snapshot.threads.find((t) => t.id !== state.selectedThread)?.id ||
        state.selectedThread,
    };
    renderModal();
  }
  if (name === "send-message") {
    const content = $("#message-content").value.trim();
    if (!content) {
      toast("请输入消息内容");
      return;
    }
    await api("/messages", {
      method: "POST",
      body: {
        session_id: state.session.id,
        target_thread_id: $("#message-target").value,
        payload: { content },
        idempotency_key: state.modal.key,
      },
    });
    state.modal = null;
    await refreshSession();
    toast("消息已保存，等待目标调用应用");
  }
  if (name === "process-messages") await sendTurn(state.selectedThread, "");
  if (name === "process-target") await sendTurn(id, "");
  if (name === "open-task-thread") {
    const task = await api(`/tasks/${id}`);
    if (state.session?.id !== task.session_id) { await loadBootstrap(); await selectSession(task.session_id); }
    await selectThread(task.thread_id);
    return;
  }
  if (name === "task-detail") {
    const modal = { type: "task", task: null, receipts: {}, receiptLoading: new Set() };
    state.modal = modal;
    renderModal();
    const task = await api(`/tasks/${id}`);
    if (state.modal === modal) {
      if (state.session?.id !== task.session_id) { await loadBootstrap(); await selectSession(task.session_id); }
      if (state.selectedThread !== task.thread_id && thread(task.thread_id)) {
        await selectThread(task.thread_id);
        state.modal = modal;
      }
      modal.task = task;
      renderModal();
    }
  }
  if (name === "task-trace") {
    const modal = state.modal;
    if (modal?.type !== "task" || !modal.task || modal.traceLoading) return;
    modal.traceLoading = true;
    modal.traceError = null;
    renderModal();
    try {
      const cursor = `?include_resources=true${modal.trace?.has_more ? `&cursor=${encodeURIComponent(modal.trace.cursor)}` : ""}`;
      const trace = await api(`/tasks/${encodeURIComponent(modal.task.id)}/trace${cursor}`);
      modal.trace = { ...trace, items: [...(modal.trace?.items || []), ...trace.items],
        tools: modal.trace?.tools || trace.tools, tools_has_more: modal.trace?.tools_has_more ?? trace.tools_has_more };
    } catch (error) { modal.traceError = error.message; }
    finally { modal.traceLoading = false; if (state.modal === modal) renderModal(); }
    return;
  }
  if (name === "tool-receipt") {
    const modal = state.modal;
    if (modal?.type !== "task" || !modal.task || modal.receiptLoading.has(id)) return;
    if (modal.receipts[id]) { delete modal.receipts[id]; renderModal(); return; }
    modal.receiptLoading.add(id);
    modal.traceError = null;
    renderModal();
    try {
      const response = await api("/rpc", { method: "POST", body: { method: "workbench.calls",
        params: { task_id: modal.task.id, tool_call_id: id } } });
      modal.receipts[id] = response.result;
    } catch (error) { modal.traceError = error.message; }
    finally { modal.receiptLoading.delete(id); if (state.modal === modal) renderModal(); }
    return;
  }
  if (name === "trace-tools") {
    const modal = state.modal;
    if (modal?.type !== "task" || !modal.trace || modal.toolsLoading) return;
    modal.toolsLoading = true;
    try {
      const { result } = await api("/rpc", { method: "POST", body: { method: "workbench.calls",
        params: { task_id: modal.task.id, cursor: modal.toolsCursor || "", limit: 50 } } });
      const calls = new Map(modal.trace.tools.map(call => [call.id, call]));
      for (const call of result.items) if (!calls.has(call.id)) calls.set(call.id, call);
      modal.trace.tools = [...calls.values()];
      modal.toolsCursor = result.next_cursor;
      modal.trace.tools_has_more = !!result.next_cursor;
    } catch (error) { modal.traceError = error.message; }
    finally { modal.toolsLoading = false; if (state.modal === modal) renderModal(); }
    return;
  }
  if (name === "continue-task") {
    const task = state.modal?.task;
    if (task?.id !== id) return;
    let request = state.continuationRequests.get(id);
    if (!request) {
      request = { key: idempotency(), runs: task.continuation_runs || 0 };
      state.continuationRequests.set(id, request);
    }
    await api(`/tasks/${encodeURIComponent(id)}/continue`, { method: "POST", body: { idempotency_key: request.key } });
    state.continuationRequests.delete(id);
    state.modal = null;
    await refreshSession();
    toast("已继续完善原任务");
    return;
  }
  if (name === "cancel-task") {
    await api(`/tasks/${id}/cancel`, { method: "POST", body: {} });
    toast("取消申请已保存");
    await refreshSession();
  }
  if (name === "retry-task") {
    await api(`/tasks/${id}/retry`, {
      method: "POST",
      body: { idempotency_key: idempotency() },
    });
    state.modal = null;
    toast("已创建新的重试任务");
    await refreshSession();
  }
  if (name === "resume-task") {
    await api(`/tasks/${id}/resume`, {
      method: "POST",
      body: {
        input: {
          wait_token: state.modal.task.wait.wait_token,
          content: $("#resume-content").value,
        },
        idempotency_key: idempotency(),
      },
    });
    state.modal = null;
    await refreshSession();
    toast("等待输入已保存");
  }
  if (name === "reconcile-task") {
    if (!$("#reconcile-confirmed").checked) {
      toast("需要先确认执行停止和外部动作的核对结果");
      return;
    }
    const resolution = $("#reconcile-resolution").value;
    await api(`/tasks/${id}/reconcile`, {
      method: "POST",
      body: {
        resolution,
        evidence: $("#reconcile-evidence").value,
        confirmed_stopped: true,
        external_actions_confirmed: true,
        safe_to_retry: resolution === "QUEUED",
      },
    });
    state.modal = null;
    await refreshSession();
    toast("核对依据与处理结果已保存");
  }
  if (name === "fork") {
    const slot = id + ":" + (element.dataset.checkpoint || "latest");
    let request = state.forkRequests.get(slot);
    if (!request) {
      request = { idempotency_key: idempotency(), checkpoint_id: element.dataset.checkpoint || thread(id)?.latest_checkpoint_id };
      state.forkRequests.set(slot, request);
    }
    const value = await api(`/threads/${encodeURIComponent(id)}/fork`, {
      method: "POST",
      body: request,
    });
    state.forkRequests.delete(slot);
    if (!thread(value.id)) state.snapshot.threads.push(value);
    await selectThread(value.id);
    toast("已从保存的结果创建分支");
  }
  if (name === "older") {
    await refreshConversation(state.selectedThread, true);
    render();
  }
  if (name === "upload") await upload();
  if (name === "resources") {
    state.resources = (await api("/resources")).resources;
    render();
  }
  if (name === "workflow") {
    if (!state.snapshot.threads.length) {
      toast("先打开 Agent 对话，再安排任务");
      return;
    }
    state.workflowRows = [
      { thread_id: state.snapshot.threads[0].id, input: "", dependency: "" },
    ];
    state.modal = { type: "workflow", key: idempotency() };
    renderModal();
  }
  if (name === "add-workflow-row") {
    state.workflowRows.push({
      thread_id:
        state.snapshot.threads[
          state.workflowRows.length % state.snapshot.threads.length
        ].id,
      input: "",
      dependency: "",
    });
    renderModal();
  }
  if (name === "remove-workflow-row") {
    const index = Number(element.dataset.index);
    state.workflowRows.splice(index, 1);
    state.workflowRows.forEach((r) => {
      if (String(r.dependency) === String(index)) r.dependency = "";
      else if (r.dependency !== "" && Number(r.dependency) > index)
        r.dependency = Number(r.dependency) - 1;
    });
    renderModal();
  }
  if (name === "submit-workflow") {
    if (
      !state.workflowRows.length ||
      state.workflowRows.some((r) => !r.input.trim())
    ) {
      toast("请为每个任务填写指令");
      return;
    }
    const nodes = state.workflowRows.map((r, i) => ({
      key: `node-${i}`,
      thread_id: r.thread_id,
      payload: { input: r.input },
      depends_on: r.dependency === "" ? [] : [`node-${r.dependency}`],
    }));
    await api("/rpc", {
      method: "POST",
      body: {
        method: "tasks.workflow",
        params: {
          session_id: state.session.id,
          nodes,
          idempotency_key: state.modal.key,
        },
      },
    });
    state.modal = null;
    state.selectedThread = null;
    state.mode = "tasks";
    await refreshSession();
    toast("任务已接收");
  }
}

const pendingActions = new Set();
const writeActions = new Set([
  "wf-confirm-delete", "wf-restore",
  "wf-new", "wf-save", "wf-archive", "wf-confirm-run", "wf-cancel-run", "wf-confirm-resume", "wf-run",
  "confirm-delete",
  "restore-deleted",
  "save-project",
  "archive-project",
  "create-session",
  "mcp-delete",
  "mcp-import-selected",
  "mcp-run-tool",
  "agent-chat",
  "new-agent-thread",
  "send-message",
  "process-messages",
  "process-target",
  "cancel-task",
  "retry-task",
  "continue-task",
  "resume-task",
  "reconcile-task",
  "fork",
  "submit-workflow",
  "save-budget",
  "delete-secret",
]);
document.addEventListener("click", async (event) => {
  if (editorInteraction(event)) return;
  if (
    event.target.classList.contains("modal-backdrop") &&
    state.modal?.type === "command"
  ) {
    state.modal = null;
    renderModal();
    return;
  }
  for (const menu of document.querySelectorAll(".context-menu[open]")) {
    if (!menu.contains(event.target)) menu.open = false;
  }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  event.preventDefault();
  const menu = button.closest(".context-menu");
  if (menu) menu.open = false;
  const key = `${button.dataset.action}:${button.dataset.id || ""}`;
  const guarded = writeActions.has(button.dataset.action);
  if (guarded && pendingActions.has(key)) return;
  if (guarded) pendingActions.add(key);
  button.setAttribute("aria-busy", "true");
  try {
    await action(button.dataset.action, button);
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (guarded) pendingActions.delete(key);
    button.removeAttribute("aria-busy");
  }
});
document.addEventListener("toggle", (event) => {
  agentLaunch.toggle(event.target);
  integrationUI.toggle(event.target);
}, true);
document.addEventListener("input", (event) => {
  const e = event.target;
  integrationUI.input(e);
  workflowUI.input(e);
  agentLaunch.input(e);
  if (e.id === "command-input" && state.modal?.type === "command") {
    state.modal.query = e.value;
    $("#command-results").innerHTML = commandResults();
  }
  if (e.id === "artifact-search") {
    state.artifactQuery = e.value;
    $("#artifact-results").innerHTML = artifactResults();
  }
  if (e.id === "chat-input") state.drafts.set(state.selectedThread, e.value);
  if (e.dataset.setting) {
    let value = e.type === "checkbox" ? e.checked : e.value;
    if (
      [
        "temperature",
        "max_output_tokens",
        "context_window_tokens",
        "max_concurrency",
        "timeout_seconds",
      ].includes(e.dataset.setting)
    )
      value = e.value === "" ? null : Number(e.value);
    if (e.dataset.setting === "reasoning_effort") value = e.value || null;
    state.settingsDraft[e.dataset.setting] = value;
    state.modelTestResult = null;
    $(".model-test-result")?.remove();
  }
  if (e.id === "session-title" && state.modal) state.modal.title = e.value;
  if (e.id === "project-name" && state.modal?.type === "project") state.modal.name = e.value;
  if (e.id === "project-folder" && state.modal?.type === "project") state.modal.local_folder = e.value;
  if (e.id === "message-content" && state.modal) state.modal.content = e.value;
  if (e.id === "resume-content" && state.modal) state.modal.resume = e.value;
  if (e.id === "reconcile-evidence" && state.modal)
    state.modal.evidence = e.value;
  if (e.dataset.workflow)
    state.workflowRows[Number(e.dataset.index)][e.dataset.workflow] = e.value;
});
document.addEventListener("change", (event) => {
  if (editorInteraction(event)) return;
  const e = event.target;
  agentLaunch.change(e);
  if (e.id?.startsWith("workbench-") || e.dataset.workbenchTool) {
    const environment = $("#workbench-environment").value;
    state.settingsDraft.workbench = environment ? {
      environment_id: environment,
      permission_profile: $("#workbench-permission").value,
      tools: [...document.querySelectorAll("[data-workbench-tool]:checked")].map(element => element.dataset.workbenchTool),
    } : null;
  }
  integrationUI.change(e).catch((error) => toast(error.message, true));
  workflowUI.change(e).catch((error) => toast(error.message, true));
  if (e.id === "workspace-select")
    selectSession(e.value).catch((error) => toast(error.message, true));
  if (e.id === "project-select")
    selectProject(e.value).catch((error) => toast(error.message, true));
  if (e.dataset.setting === "kind" || e.dataset.setting === "llm_required") {
    state.settingsDraft[e.dataset.setting] = e.type === "checkbox" ? e.checked : e.value;
    state.modelTestResult = null;
    render();
  }
  if (e.id === "message-target" && state.modal) state.modal.target = e.value;
  if (e.id === "reconcile-resolution" && state.modal)
    state.modal.resolution = e.value;
  if (e.id === "reconcile-confirmed" && state.modal)
    state.modal.confirmed = e.checked;
  if (e.dataset.workflow)
    state.workflowRows[Number(e.dataset.index)][e.dataset.workflow] = e.value;
});
document.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    if (await integrationUI.submit(event)) return;
    if (event.target.id === "composer") {
      const content = $("#chat-input").value.trim();
      if (content) await sendTurn(state.selectedThread, content);
    }
    if (event.target.id === "agent-form") {
      if (state.savingSettings) return;
      const d = state.settingsDraft,
        config = {};
      for (const key of [
        "id",
        "name",
        "project_id",
        "kind",
        "enabled",
        "llm_required",
        "base_url",
        "model",
        "system_prompt",
        "agent_md",
        "skills",
        "skill_bindings",
        "mcp_bindings",
        "workbench",
        "temperature",
        "reasoning_effort",
        "max_output_tokens",
        "context_window_tokens",
        "context_budget_tokens",
        "token_parameter",
        "timeout_seconds",
        "max_concurrency",
        "collaboration",
        "command",
        "process_descriptor",
      ])
        if (d[key] !== undefined) config[key] = d[key];
      if (!config.id || !config.name) {
        toast("填写 Agent ID 和显示名称");
        return;
      }
      if (d.kind === "process") config.command = agentLaunch.commit(d);
      if (d.api_key) config.api_key = d.api_key;
      const submittedDraft = JSON.stringify(d);
      state.savingSettings = true;
      render();
      try {
        const value = await api(`/agents/${encodeURIComponent(config.id)}`, {
          method: "PUT",
          body: config,
        });
        if (state.settingsDraft === d && JSON.stringify(d) === submittedDraft) {
          state.settingsId = value.id;
          state.settingsDraft = { ...value, api_key: "" };
        }
        await loadBootstrap();
        toast(value.model_setup?.state === "needs_configuration" ? "Agent 已保存，请继续配置模型 API" : "Agent 配置已保存");
      } finally {
        state.savingSettings = false;
        render();
      }
    }
  } catch (error) {
    toast(error.message, true);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    const menu = document.querySelector(".context-menu[open]");
    if (menu) { menu.open = false; menu.querySelector("summary").focus(); event.preventDefault(); return; }
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    // Preserve unfinished forms; Escape closes them explicitly.
    if (!state.modal || state.modal.type === "command") {
      action("command", { dataset: {} }).catch((error) =>
        toast(error.message, true),
      );
    }
    return;
  }
  if (event.key === "Escape" && state.modal) {
    if (state.modal.pending) return;
    event.preventDefault();
    state.modal = null;
    renderModal();
    return;
  }
  if (event.key === "Escape" && state.inspectorOpen) {
    state.inspectorOpen = false;
    render();
    $("[data-action=toggle-inspector]")?.focus();
    return;
  }
  if (state.modal?.type === "command" && !event.isComposing) {
    const results = [...document.querySelectorAll(".command-result")];
    const index = results.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp"].includes(event.key) && results.length) {
      event.preventDefault();
      const next =
        event.key === "ArrowDown"
          ? (index + 1) % results.length
          : index <= 0
            ? results.length - 1
            : index - 1;
      results[next].focus();
    }
    if (event.key === "Enter" && event.target.id === "command-input") {
      event.preventDefault();
      results[0]?.click();
    }
  }
  if (
    event.target.id === "session-title" &&
    event.key === "Enter" &&
    !event.isComposing
  ) {
    event.preventDefault();
    $("[data-action=create-session]")?.click();
  }
  if (
    event.target.id === "chat-input" &&
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing
  ) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
  if (event.key === "Tab" && state.modal) {
    const elements = [
      ...$("#modal-root").querySelectorAll(
        "button,input,textarea,select,a[href]",
      ),
    ].filter((e) => !e.disabled && e.getClientRects().length);
    if (elements.length) {
      const first = elements[0],
        last = elements.at(-1);
      if (
        event.shiftKey &&
        (document.activeElement === first ||
          !elements.includes(document.activeElement))
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last ||
          !elements.includes(document.activeElement))
      ) {
        event.preventDefault();
        first.focus();
      }
    }
  }
});
async function boot() {
  try {
    await loadBootstrap();
    const saved = localStorage.getItem("ma-session");
    const target =
      state.sessions.find((s) => s.id === saved) ||
      state.sessions.find((s) => s.state === "OPEN");
    await selectSession(target?.id);
    if (state.route === "workflows") await workflowUI.load();
  } catch (error) {
    state.connection = "offline";
    render();
    toast(error.message, true);
  }
}
window.addEventListener("hashchange", () => {
  const route = location.hash.slice(1);
  if (
    ["workspace", "mcp", "skills", "agents", "settings", "workflows"].includes(route) &&
    route !== state.route
  ) {
    state.route = route;
    state.modal = null;
    render();
    if (route === "workflows") workflowUI.load().catch((error) => toast(error.message, true));
  }
});
// A browser network change prompts one local check; loopback may remain usable
// while the machine is offline. SSE errors still detect host-only outages.
window.addEventListener("offline", () => reconnect().catch(() => {}));
window.addEventListener("online", () => reconnect().catch(() => {}));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") render();
});
boot();
