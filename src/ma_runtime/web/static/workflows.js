import { content, schemaView, schemaDefaults, valueEditor, readEditor } from "./presentation.mjs";
import { orderedRuns } from "./instance-view.mjs";
import { autoLayout, canConnect, clampZoom, clone, curve, fitViewport, portPoint } from "./workflow-graph.mjs";

export function createWorkflowUI({ state, escape: esc, icon, badge, markdown, api, toast, render, idempotency, time, ensureSession }) {
  const $ = (selector) => document.querySelector(selector);
  const labels = { start: "开始", agent: "Agent", map: "并行实例", condition: "条件分支", template: "模板转换", end: "结束" };
  const symbols = { start: "arrow", agent: "agents", map: "agents", condition: "branch", template: "file", end: "check" };
  const done = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
  const projects = new Map();
  let ws = fresh(null), drag = null;
  function fresh(projectId) {
    return { projectId, items: [], cursor: null, more: false, archived: false, deletedOnly: false, loading: false, loaded: false,
      draft: null, buffers: new Map(), dirty: false, selected: null, edge: null, types: [], errors: {},
      undo: [], redo: [], validation: null, linking: null, mode: "canvas", runs: [], runCursor: null, runsMore: false,
      run: null, activity: [], selectionEpoch: 0, dialog: null, runInput: '{\n  "input": ""\n}', pendingRuns: new Map(), pendingCreate: null,
      timer: null, epoch: 0, conflict: false, backup: null, inspector: true };
  }
  const graph = () => ws.mode === "history" && ws.run ? ws.run.graph : ws.draft?.graph;
  const editable = () => ws.mode === "canvas" && ws.draft?.state === "ACTIVE";
  const selected = () => graph()?.nodes.find((node) => node.id === ws.selected);
  const keyFor = (node, field) => `${node.id}:${field}`;
  const json = (value) => JSON.stringify(value, null, 2);
  const call = async (method, params = {}) => (await api("/rpc", { method: "POST", body: { method: "workflows." + method, params } })).result;
  function stash() {
    if (ws.draft) ws.buffers.set(ws.draft.id, { draft: clone(ws.draft), dirty: ws.dirty,
      errors: { ...ws.errors }, undo: clone(ws.undo), redo: clone(ws.redo) });
  }
  function snapshot() { return { graph: clone(ws.draft.graph), name: ws.draft.name, description: ws.draft.description, errors: { ...ws.errors } }; }
  function remember() {
    ws.undo.push(snapshot());
    if (ws.undo.length > 60) ws.undo.shift();
    ws.redo = [];
  }
  function dirty() {
    ws.dirty = true;
    ws.validation = null;
    const indicator = $("#wf-save-state");
    if (indicator) indicator.textContent = "有未保存的修改";
    stash();
  }
  function use(value) {
    stash();
    const buffer = ws.buffers.get(value.id);
    const working = !!buffer?.dirty && value.state !== "DELETED";
    ws.draft = clone(working ? buffer.draft : value);
    ws.draft.state = value.state;
    ws.dirty = working;
    ws.errors = working ? { ...buffer.errors } : {};
    ws.undo = working ? clone(buffer.undo) : [];
    ws.redo = working ? clone(buffer.redo) : [];
    ws.selected = ws.draft.graph.nodes[0]?.id || null;
    ws.edge = null;
    ws.validation = ws.draft.validation;
    ws.conflict = false;
    ws.mode = "canvas";
    ws.run = null;
    ws.runs = [];
    ws.runCursor = null;
    ws.runsMore = false;
    ws.linking = null;
    ws.needsFit = true;
  }
  function clearEditor(target = ws) {
    clearTimeout(target.timer);
    Object.assign(target, { draft: null, dirty: false, errors: {}, undo: [], redo: [],
      selected: null, edge: null, run: null, runs: [], runCursor: null, runsMore: false,
      dialog: null, timer: null, linking: null, conflict: false, validation: null });
  }

  async function load(projectId = state.project?.id, force = false) {
    const switched = projectId !== ws.projectId;
    if (projectId !== ws.projectId) {
      stash();
      clearTimeout(ws.timer);
      ws.timer = null;
      projects.set(ws.projectId, ws);
      ws = projects.get(projectId) || fresh(projectId);
    }
    if (!projectId || (ws.loaded && !force)) { if (switched) render(); return; }
    const current = ws, epoch = ++ws.epoch;
    ws.loading = true;
    render();
    try {
      const [page, types, activity] = await Promise.all([call("list", { project_id: projectId, include_archived: ws.archived, ...(ws.deletedOnly ? { deleted_only: true } : {}) }), call("node_types"), call("activity", { project_id: projectId })]);
      if (ws !== current || ws.epoch !== epoch) return;
      ws.items = page.items;
      ws.cursor = page.cursor;
      ws.more = page.has_more;
      ws.types = types;
      ws.activity = activity;
      ws.loaded = true;
      ws.unavailable = false;
      if ((!ws.draft && page.items.length) || (ws.draft && !ws.dirty)) {
        const identity = ws.draft?.id || page.items[0].id;
        const updated = await call("get", { workflow_id: identity });
        if (ws !== current || ws.epoch !== epoch) return;
        if (!ws.draft) {
          use(updated);
          if (ws.activity.some(item => item.workflow_id === identity && item.active_count)) await history();
        }
        else ws.draft = clone(updated);
      }
    } catch (error) {
      if (ws !== current || ws.epoch !== epoch) return;
      if (error.code === "METHOD_NOT_FOUND" || (ws.deletedOnly && error.code === "INVALID_REQUEST")) ws.unavailable = true;
      else throw error;
    } finally {
      if (ws === current && ws.epoch === epoch) { ws.loading = false; render(); }
    }
  }

  function page() {
    if (!state.project) return `<section class="panel-page">${empty("先选择一个项目", "每个工作流只属于一个项目。创建项目后，即可在画布上组织工作。", "new-project", "创建项目")}</section>`;
    if (ws.projectId !== state.project.id) return '<section class="wf-page"><p class="wf-muted">正在加载当前项目的工作流…</p></section>';
    if (ws.unavailable) return `<section class="panel-page">${empty("工作流引擎等待加载", "请在现有任务处理完成后重启平台服务，再重连 Codex MCP，以加载新的工作流能力。", "wf-check-engine", "重新检查")}</section>`;
    return `<section class="wf-page"><div class="wf-heading"><div><p class="eyebrow">WORKFLOW STUDIO</p><h1>工作流</h1><p>添加步骤 → 连接输入与输出 → 校验后运行。每次运行都保留独立记录。</p></div><button class="btn primary" data-action="wf-new">${icon("plus")}新建工作流</button></div>
      <div class="wf-workspace"><aside class="wf-library" aria-label="项目工作流"><header>${libraryHeader()}</header>
        <div class="wf-library-list">${ws.loading && !ws.loaded ? '<p class="wf-muted">正在加载…</p>' : ws.items.map(libraryItem).join("") || '<div class="wf-library-empty">从一个目标开始，<br>把步骤放上画布。</div>'}</div>
        ${ws.more ? '<button class="btn small" data-action="wf-more">加载更多</button>' : ""}<footer><button class="text-link" data-action="wf-import">${icon("plus")}导入工作流</button><input type="file" id="wf-import-file" accept="application/json,.json" hidden><p>${icon("folder")}归属当前项目</p></footer></aside>
        ${ws.draft ? editor() : ws.deletedOnly ? empty("工作流回收站为空", "删除的工作流可在这里恢复，画布与运行记录会一并保留。", "wf-trash", "返回项目工作流") : empty("画布已就绪", "添加节点、连接步骤，再把结果交给下一位 Agent。", "wf-new", "创建第一个工作流")}</div>${dialog()}</section>`;
  }
  function empty(title, description, action, label) {
    return `<div class="empty wf-empty">${icon("flow")}<h3>${esc(title)}</h3><p>${esc(description)}</p><button class="btn primary" data-action="${action}">${icon("plus")}${esc(label)}</button></div>`;
  }
  function libraryHeader() {
    return `<strong>${ws.deletedOnly ? "工作流回收站" : "项目工作流"} <span>${ws.items.length}</span></strong><div class="wf-library-tools"><button class="icon-btn ${ws.archived && !ws.deletedOnly ? "active" : ""}" data-action="wf-archived" aria-label="${ws.archived && !ws.deletedOnly ? "隐藏" : "显示"}已归档工作流" title="已归档工作流">${icon("folder")}</button><button class="icon-btn ${ws.deletedOnly ? "active" : ""}" data-action="wf-trash" aria-label="${ws.deletedOnly ? "返回项目工作流" : "工作流回收站"}" title="${ws.deletedOnly ? "返回项目工作流" : "工作流回收站"}">${icon("trash")}</button></div>`;
  }
  function workflowActions(readOnly) {
    return `<button class="icon-btn" data-action="wf-export" aria-label="导出工作流" title="导出工作流">${icon("download")}</button>${ws.draft.state === "DELETED"
      ? `<button class="btn primary small" data-action="wf-restore">${icon("refresh")}恢复工作流</button>`
      : `<button class="icon-btn danger-icon" data-action="wf-delete" aria-label="删除工作流" title="删除工作流">${icon("trash")}</button><button class="btn small" data-action="wf-archive">${ws.draft.state === "ARCHIVED" ? "恢复" : "归档"}</button>`}
      <button class="btn small" data-action="wf-validate" ${readOnly ? "disabled" : ""}>${icon("shield")}校验</button><button class="btn small" data-action="wf-save" ${readOnly ? "disabled" : ""}>${icon("check")}保存</button><button class="btn primary small" data-action="wf-run" ${readOnly ? "disabled" : ""}>${icon("arrow")}运行</button>`;
  }
  function editor() {
    const readOnly = !editable();
    return `<section class="wf-editor" aria-label="工作流编辑器"><header class="wf-editor-header"><div class="wf-name-wrap"><input id="wf-name" data-focus="wf-name" data-wf-meta="name" aria-label="工作流名称" maxlength="120" value="${esc(ws.draft.name)}" ${readOnly ? "readonly" : ""}><small id="wf-save-state">${ws.draft.state === "DELETED" ? `已删除 · r${ws.draft.revision}` : ws.dirty ? "有未保存的修改" : `已保存 · r${ws.draft.revision}`}</small></div><div class="wf-editor-actions">${workflowActions(readOnly)}</div></header>
      <nav class="wf-tabs" aria-label="工作流视图"><button class="${ws.mode === "canvas" ? "active" : ""}" data-action="wf-mode" data-mode="canvas">${icon("flow")}画布</button><button class="${ws.mode === "history" ? "active" : ""}" data-action="wf-mode" data-mode="history">${icon("clock")}运行记录</button><span>项目归属：${esc(state.project.name)}</span></nav>
      ${ws.conflict ? '<div class="wf-notice error">已保存版本已更新。当前草稿保留，请导出或重新加载后合并。<button class="text-link" data-action="wf-reload">重新加载</button></div>' : ""}
      ${ws.mode === "canvas" ? palette() : historyStrip()}
      <div class="wf-edit-body ${ws.inspector ? "inspector-open" : ""}">${canvas()}<aside class="wf-inspector" id="wf-inspector" aria-label="节点属性">${inspector()}</aside></div>
      ${ws.validation && ws.mode === "canvas" ? validation() : ""}
      <footer class="wf-footer"><span>${icon("folder")}${esc(state.project.name)}<i>·</i>${graph()?.nodes.length || 0} 个节点<i>·</i>${graph()?.edges.length || 0} 条连线</span><span>${ws.mode === "history" ? "运行快照 · 只读" : "拖动节点 · 从输出端口连接到输入端口"}</span></footer></section>`;
  }
  function palette() {
    return `<div class="wf-palette"><span>添加节点</span>${Object.entries(labels).map(([kind, label]) => `<button class="wf-add-node ${kind}" data-action="wf-add-node" data-kind="${kind}" ${!editable() || (kind === "start" && ws.draft.graph.nodes.some((node) => node.type === "start")) ? "disabled" : ""}>${icon(symbols[kind])}${label}</button>`).join("")}<div class="wf-palette-end"><button class="icon-btn" data-action="wf-undo" aria-label="撤销" title="撤销 · Ctrl Z" ${!editable() || !ws.undo.length ? "disabled" : ""}>↶</button><button class="icon-btn" data-action="wf-redo" aria-label="重做" title="重做 · Ctrl Shift Z" ${!editable() || !ws.redo.length ? "disabled" : ""}>↷</button><button class="icon-btn" data-action="wf-layout" aria-label="自动布局" title="自动布局" ${!editable() ? "disabled" : ""}>${icon("grid")}</button><button class="icon-btn" data-action="wf-toggle-inspector" aria-label="切换属性面板" title="属性面板">${icon("panel")}</button></div></div>`;
  }
  function historyStrip() {
    return `<div class="wf-history-strip"><span class="wf-run-summary">${ws.run ? `${ws.runs.length}${ws.runsMore ? "+" : ""} 个运行实例 · 当前 r${ws.run.revision}` : "还没有运行记录"}</span>
      <div class="wf-run-selector"><label for="wf-run-choice">运行实例</label><select id="wf-run-choice" data-focus="wf-run-choice" aria-label="切换工作流运行实例">
      ${orderedRuns(ws.runs).map((run, index) => `<option value="${esc(run.id)}" ${ws.run?.id === run.id ? "selected" : ""}>${index + 1} · ${statusText(run.state)} · r${run.revision} · ${date(run.created_at)}</option>`).join("")}</select></div>
      ${ws.runsMore ? '<button class="btn small" data-action="wf-more-runs">更多记录</button>' : ""}<button class="icon-btn" data-action="wf-refresh-runs" aria-label="刷新运行记录">${icon("refresh")}</button>
      ${ws.run && !done.has(ws.run.state) ? '<button class="btn small" data-action="wf-cancel-run">取消当前运行</button>' : ""}<button class="icon-btn" data-action="wf-toggle-inspector" aria-label="切换属性面板">${icon("panel")}</button></div>`;
  }
  const nodeBadge = value => value === "PENDING" ? '<span class="badge PENDING">等待前置</span>' : badge(value);
  const statusText = value => ({ RUNNING: "执行中", WAITING: "等待", UNCERTAIN: "待核对", CANCELLING: "正在取消", FAILING: "正在停止", SUCCEEDED: "完成", FAILED: "失败", CANCELLED: "已取消" }[value] || value);
  const date = stamp => new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(stamp));
  function libraryItem(item) {
    const activity = ws.activity.find(value => value.workflow_id === item.id), count = activity?.active_count || 0;
    return `<button class="wf-library-item ${ws.draft?.id === item.id ? "active" : ""} ${count ? "has-running" : ""} ${count > 1 ? "is-stacked" : ""}" data-action="wf-open" data-id="${esc(item.id)}">
      <span class="wf-library-symbol">${icon("flow")}</span><span><strong>${esc(ws.buffers.get(item.id)?.dirty ? ws.buffers.get(item.id).draft.name : item.name)}${ws.buffers.get(item.id)?.dirty ? '<i class="wf-dirty-dot"></i>' : ""}</strong>
      <small>${item.state === "DELETED" ? "已删除 · " : item.state === "ARCHIVED" ? "已归档 · " : ""}r${item.revision} · ${(item.node_count ?? item.graph?.nodes.length ?? 0)} 个节点${count ? ` · ${count} 运行` : ""}</small></span>${count > 1 ? `<span class="instance-count">${count}</span>` : ""}</button>`;
  }

  function canvas() {
    const current = graph();
    if (!current) return '<div class="wf-canvas"></div>';
    const view = current.viewport || { x: 0, y: 0, zoom: 1 };
    return `<div class="wf-canvas ${ws.linking ? "connecting" : ""}" id="wf-canvas" role="region" tabindex="0" aria-label="工作流画布，拖动节点和端口进行编排"><div class="wf-world" id="wf-world"><svg class="wf-edges" width="1" height="1" aria-label="工作流连线"><defs><marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs><g id="wf-edge-paths">${edges()}</g><path class="wf-connection-preview" id="wf-preview"/></svg>${current.nodes.map(nodeCard).join("")}</div>
      ${ws.linking ? '<div class="wf-connect-hint" role="status">选择目标节点的左侧输入端口 <button class="text-link" data-action="wf-stop-link">取消连线</button></div>' : ""}
      <div class="wf-canvas-controls"><button class="icon-btn" data-action="wf-zoom-out" aria-label="缩小">−</button><span id="wf-zoom-label">${Math.round(view.zoom * 100)}%</span><button class="icon-btn" data-action="wf-zoom-in" aria-label="放大">+</button><span></span><button class="btn small" data-action="wf-fit">适应画布</button></div><span class="wf-canvas-caption">${ws.mode === "history" ? `RUN SNAPSHOT / r${ws.run?.revision || "—"}` : "连接前一步的输出，作为下一步的输入"}</span></div>`;
  }
  function edges() {
    const current = graph();
    return current.edges.map((edge) => {
      const from = current.nodes.find((node) => node.id === edge.source), to = current.nodes.find((node) => node.id === edge.target);
      if (!from || !to) return "";
      const start = portPoint(from, edge.source_handle), end = portPoint(to, "default", true), d = curve(start, end);
      const sourceState = ws.mode === "history" ? ws.run?.nodes.find((node) => node.node_id === edge.source) : null;
      const active = sourceState?.state === "SUCCEEDED" && (edge.source_handle === "default" || sourceState.branch === edge.source_handle);
      return `<g data-edge-id="${esc(edge.id)}" class="wf-edge ${ws.edge === edge.id ? "selected" : ""} ${active ? "executed" : ""}"><path class="wf-edge-hit" d="${d}" data-action="wf-select-edge" data-id="${esc(edge.id)}"/><path class="wf-edge-line" d="${d}" marker-end="url(#wf-arrow)"/>${edge.source_handle && edge.source_handle !== "default" ? `<text x="${start.x + 26}" y="${start.y - 9}">${edge.source_handle === "true" ? "是" : "否"}</text>` : ""}</g>`;
    }).join("");
  }
  function summary(node) {
    if (node.type === "start") return "定义流程的输入变量";
    if (node.type === "end") return Object.keys(node.config.outputs || {}).join(" · ") || "配置最终输出";
    if (node.type === "agent") return state.agents.find((agent) => agent.id === node.config.agent_id)?.name || "选择项目内的 Agent";
    if (node.type === "map") return `${node.config.agent_ids.map(id => state.agents.find(a => a.id === id)?.name || id).join(" / ") || "选择 Agent 池"} · 最多 ${node.config.max_concurrency} 并行`;
    if (node.type === "condition") return `${displayValue(node.config.left)} ${operators[node.config.operator] || ""} ${["is_empty", "is_not_empty"].includes(node.config.operator) ? "" : displayValue(node.config.right)}`;
    return node.config.template || "编写文本模板";
  }
  const displayValue = (value) => typeof value === "string" ? value : JSON.stringify(value);
  function nodeCard(node) {
    const execution = ws.mode === "history" ? ws.run?.nodes.find((item) => item.node_id === node.id) : null;
    return `<article class="wf-node ${esc(node.type)} ${ws.selected === node.id ? "selected" : ""} ${execution ? esc(execution.state) : ""}" data-node-id="${esc(node.id)}" aria-label="${esc(node.title || labels[node.type])}"><button class="wf-node-head" data-action="wf-select-node" data-id="${esc(node.id)}"><span class="wf-node-symbol">${icon(symbols[node.type])}</span><strong>${esc(node.title || labels[node.type])}</strong><span class="wf-node-state" title="${esc(execution?.state || "")}">${execution?.state === "SUCCEEDED" ? icon("check") : execution?.state === "RUNNING" ? icon("activity") : ""}</span></button><p title="${esc(summary(node))}">${esc(summary(node))}</p><footer><span>${esc(node.id)}</span>${execution ? nodeBadge(execution.state) : `<span>${labels[node.type]}</span>`}</footer>
      ${node.type !== "start" ? `<button class="wf-port input" data-action="wf-target-port" data-id="${esc(node.id)}" aria-label="${esc(node.title || labels[node.type])} 输入端口" title="输入" ${!editable() ? "disabled" : ""}></button>` : ""}
      ${node.type !== "end" ? (node.type === "condition" ? ["true", "false"] : ["default"]).map((handle) => `<button class="wf-port output ${handle}" data-action="wf-source-port" data-id="${esc(node.id)}" data-handle="${handle}" aria-label="${esc(node.title || labels[node.type])} ${handle === "true" ? "是" : handle === "false" ? "否" : "输出"}端口" title="${handle === "true" ? "是" : handle === "false" ? "否" : "输出"}" ${!editable() ? "disabled" : ""}></button>`).join("") : ""}</article>`;
  }
  const operators = { equals: "等于", not_equals: "不等于", contains: "包含", gt: "大于", gte: "大于等于", lt: "小于", lte: "小于等于", is_empty: "为空", is_not_empty: "非空" };
  function textField(label, field, value, { textarea = false, type = "text", hint = "" } = {}) {
    const id = `wf-config-${field}`;
    return `<label class="field">${label}${textarea ? `<textarea id="${id}" data-focus="${id}" data-wf-field="${field}">${esc(value ?? "")}</textarea>` : `<input id="${id}" data-focus="${id}" data-wf-field="${field}" type="${type}" value="${esc(value ?? "")}">`}${hint ? `<small>${hint}</small>` : ""}</label>`;
  }
  function jsonField(label, field, value) {
    return `<div class="field"><span>${esc(label)}</span>${valueEditor(value, "wf-" + field)}<small class="wf-field-error" id="wf-error-${field}" role="status">${esc(ws.errors[keyFor(selected(), field)] || "")}</small></div>`;
  }
  function inputFields(schema) {
    return `<p class="form-hint">设置用户在运行前需要提供的信息。</p><div class="wf-input-fields">${Object.entries(schema.properties || {}).map(([key, field]) => `<div class="wf-input-field"><label class="field">变量名称<input data-wf-schema="name" data-key="${esc(key)}" data-focus="wf-schema-${esc(key)}-name" value="${esc(key)}" required></label><label class="field">显示名称<input data-wf-schema="title" data-key="${esc(key)}" data-focus="wf-schema-${esc(key)}-title" value="${esc(field.title || "")}" placeholder="例如：工作目标"></label><label class="field">内容类型<select data-wf-schema="type" data-key="${esc(key)}">${Object.entries({ string: "文本", number: "数字", integer: "整数", boolean: "开关", array: "列表", object: "字段组" }).map(([type, label]) => `<option value="${type}" ${field.type === type ? "selected" : ""}>${label}</option>`).join("")}</select></label><label class="check-row"><input type="checkbox" data-wf-schema="required" data-key="${esc(key)}" ${(schema.required || []).includes(key) ? "checked" : ""}>运行前必填</label><button class="text-link danger-text" data-action="wf-remove-input" data-key="${esc(key)}">移除此变量</button></div>`).join("")}</div><button class="text-link" data-action="wf-add-input">${icon("plus")}添加输入变量</button><details class="wf-advanced"><summary>输入结构高级设置</summary>${jsonField("输入结构", "input_schema", schema)}</details>`;
  }
  function inspector() {
    if (ws.edge) {
      const edge = graph().edges.find((item) => item.id === ws.edge);
      return `<header><h2>连线</h2><button class="icon-btn wf-inspector-close" data-action="wf-toggle-inspector" aria-label="关闭属性面板">${icon("close")}</button></header><div class="wf-inspector-body"><p>${esc(edge?.source)} ${icon("arrow")} ${esc(edge?.target)}</p><p class="wf-muted">${edge?.source_handle === "true" ? "条件为是" : edge?.source_handle === "false" ? "条件为否" : "前置步骤完成后继续"}</p>${editable() ? `<button class="btn danger" data-action="wf-delete-edge" data-id="${esc(ws.edge)}">${icon("trash")}删除连线</button>` : ""}</div>`;
    }
    const node = selected();
    if (!node) return `<header><h2>${ws.mode === "history" ? "运行概览" : "工作流属性"}</h2></header><div class="wf-inspector-body">${ws.mode === "history" ? runOverview() : `<p class="wf-muted">选择节点配置输入和输出。可从端口拖动连线，也可使用下方表单。</p>${textField("工作流说明", "description", ws.draft.description, { textarea: true })}${connectionForm()}`}</div>`;
    const execution = ws.mode === "history" ? ws.run?.nodes.find((item) => item.node_id === node.id) : null;
    return `<header><span class="wf-node-symbol ${node.type}">${icon(symbols[node.type])}</span><h2>${labels[node.type]}</h2><button class="icon-btn wf-inspector-close" data-action="wf-toggle-inspector" aria-label="关闭属性面板">${icon("close")}</button></header><div class="wf-inspector-body">${execution ? runNode(node, execution) : !editable() ? `<h3>${esc(node.title)}</h3>${content(node.config)}` : `${textField("节点名称", "title", node.title)}<span class="wf-node-id">${esc(node.id)}</span>${configPanel(node)}${variables(node)}${connections(node)}<details class="wf-advanced"><summary>位置与连接</summary><div class="form-grid">${textField("X", "x", node.position.x, { type: "number" })}${textField("Y", "y", node.position.y, { type: "number" })}</div>${connectionForm(node)}</details><div class="wf-node-actions"><button class="btn small" data-action="wf-duplicate" ${node.type === "start" ? "disabled" : ""}>复制节点</button><button class="btn small danger" data-action="wf-delete-node">${icon("trash")}删除</button></div>`}</div>`;
  }
  function configPanel(node) {
    const config = node.config;
    if (node.type === "start") return `${inputFields(config.input_schema)}<label class="field">分支失败时<select data-wf-field="failure_policy"><option value="fail_fast" ${config.failure_policy !== "collect_independent" ? "selected" : ""}>停止本次运行</option><option value="collect_independent" ${config.failure_policy === "collect_independent" ? "selected" : ""}>继续独立分支，保留各自结果</option></select></label>`;
    if (node.type === "agent") return `${executionPolicies(config)}<label class="field">执行 Agent<select id="wf-agent" data-focus="wf-agent" data-wf-field="agent_id"><option value="">选择当前项目的 Agent</option>${state.agents.filter((agent) => agent.kind !== "external").map((agent) => `<option value="${esc(agent.id)}" ${config.agent_id === agent.id ? "selected" : ""}>${esc(agent.name)}</option>`).join("")}</select></label>${jsonField("任务输入", "payload", config.payload)}<p class="form-hint">使用该 Agent 的模型配置和工具授权。</p><details class="wf-advanced"><summary>超时与重试</summary>${textField("超时（秒）", "attempt_timeout_s", config.attempt_timeout_s, { type: "number" })}${textField("最多尝试次数", "max_attempts", config.max_attempts, { type: "number" })}<label class="field">重试策略<select data-wf-field="retry_policy"><option value="NEVER" ${config.retry_policy === "NEVER" ? "selected" : ""}>不自动重试</option><option value="SAFE_ONLY" ${config.retry_policy === "SAFE_ONLY" ? "selected" : ""}>仅执行端确认安全时重试</option></select></label></details>`;
    if (node.type === "map") return `${executionPolicies(config)}${jsonField("参与执行的 Agent ID", "agent_ids", config.agent_ids)}${jsonField("输入列表或变量引用", "items", config.items)}${jsonField("每个实例的输入", "payload", config.payload)}${textField("批次并发上限", "max_concurrency", config.max_concurrency, { type: "number" })}<label class="field">路由方式<select data-wf-field="routing"><option value="round_robin" ${config.routing === "round_robin" ? "selected" : ""}>轮询分配</option><option value="least_loaded" ${config.routing === "least_loaded" ? "selected" : ""}>按负载分配</option></select></label><p class="form-hint">最多 128 项。payload 可引用 {{item}}、{{index}} 和前置变量。仍受全局与 Agent 预算限制。</p><label class="field">成员结果<select data-wf-field="result_mode"><option value="all_succeeded" ${config.result_mode !== "all_settled" ? "selected" : ""}>全部成功后汇总 outputs</option><option value="all_settled" ${config.result_mode === "all_settled" ? "selected" : ""}>收集每项状态与结果 results</option></select></label>`;
    if (node.type === "condition") return `${textField("左侧值", "left", displayValue(config.left))}<label class="field">判断条件<select data-wf-field="operator" data-focus="wf-operator">${Object.entries(operators).map(([op, title]) => `<option value="${op}" ${config.operator === op ? "selected" : ""}>${title}</option>`).join("")}</select></label>${["is_empty", "is_not_empty"].includes(config.operator) ? "" : textField("右侧值", "right", displayValue(config.right))}<p class="form-hint">可填写变量、文本、数字或布尔值。将“是”和“否”出口分别连接到后续节点。</p>`;
    if (node.type === "template") return `${textField("文本模板", "template", config.template, { textarea: true })}<p class="form-hint">通过 {{inputs.name}} 或 {{nodes.node_id.text}} 引用变量。输出保存在 text 字段。</p>`;
    return `${jsonField("最终交付字段", "outputs", config.outputs)}<p class="form-hint">完整变量引用保留原始类型。引用可能被跳过的分支时，可添加 $ref 引用字段与 default 默认值。</p>`;
  }
  function executionPolicies(config) {
    return `<details class="wf-advanced"><summary>工具与 Skill 使用规则</summary><p class="form-hint">默认自动选择。可禁用、限定范围，或要求成功调用工具和加载指定 Skill；规则只能收窄 Agent 已有授权。</p>${jsonField("工具规则 · mode / allowed / required", "tool_policy", config.tool_policy || { mode: "auto" })}${jsonField("Skill 规则 · mode / allowed / required", "skill_policy", config.skill_policy || { mode: "auto" })}</details>`;
  }
  function variables(node) {
    const upstream = new Set(), pending = [node.id];
    while (pending.length) {
      const current = pending.pop();
      for (const edge of graph().edges.filter((item) => item.target === current)) if (!upstream.has(edge.source)) { upstream.add(edge.source); pending.push(edge.source); }
    }
    const start = graph().nodes.find((item) => item.type === "start");
    const names = Object.keys(start?.config.input_schema?.properties || {});
    const refs = ["inputs", ...((names.length ? names : ["input"]).map((name) => "inputs." + name)),
      ...graph().nodes.filter((item) => upstream.has(item.id)).map((item) => `nodes.${item.id}${["agent", "template"].includes(item.type) ? ".text" : item.type === "condition" ? ".result" : item.type === "map" ? item.config.result_mode === "all_settled" ? ".results" : ".outputs" : ""}`)];
    if (node.type === "map") refs.push("item", "index");
    return `<details class="wf-variables"><summary>可用变量 <span>${refs.length}</span></summary><p class="form-hint">点击变量，插入当前输入框。</p>${refs.map((ref) => `<button class="wf-variable" data-action="wf-insert-variable" data-ref="${esc(ref)}">${esc(ref)}</button>`).join("")}</details>`;
  }
  function connections(node) {
    const edges = graph().edges.filter((edge) => edge.source === node.id || edge.target === node.id);
    return `<div class="wf-connections"><h3>连接 <span>${edges.length}</span></h3>${edges.map((edge) => `<div><span>${esc(edge.source)} → ${esc(edge.target)}${edge.source_handle === "true" ? " · 是" : edge.source_handle === "false" ? " · 否" : ""}</span><button class="icon-btn" data-action="wf-delete-edge" data-id="${esc(edge.id)}" aria-label="删除 ${esc(edge.source)} 到 ${esc(edge.target)} 的连线">${icon("close")}</button></div>`).join("") || '<p class="form-hint">还没有连接。</p>'}</div>`;
  }
  function connectionForm(node) {
    const current = graph();
    return `<div class="wf-link-form"><label class="field">从节点<select id="wf-link-source" aria-label="连线起点">${current.nodes.filter((item) => item.type !== "end").map((item) => `<option value="${esc(item.id)}" ${item.id === node?.id ? "selected" : ""}>${esc(item.title || item.id)}</option>`).join("")}</select></label><label class="field">输出端口<select id="wf-link-handle" aria-label="输出端口"><option value="default">默认输出</option><option value="true" ${node?.type === "condition" ? "selected" : ""}>是</option><option value="false">否</option></select></label><label class="field">到节点<select id="wf-link-target" aria-label="连线终点">${current.nodes.filter((item) => item.type !== "start").map((item) => `<option value="${esc(item.id)}">${esc(item.title || item.id)}</option>`).join("")}</select></label><button class="btn small" data-action="wf-connect">${icon("plus")}添加连线</button></div>`;
  }
  function validation() {
    const report = ws.validation;
    const issues = [...report.errors, ...report.warnings];
    return `<div class="wf-validation ${report.valid ? "valid" : "error"}" role="status"><div>${icon(report.valid ? "check" : "shield")}<strong>${report.valid ? "校验通过" : `${report.errors.length} 项需要调整`}</strong><button class="icon-btn" data-action="wf-dismiss-validation" aria-label="收起校验结果">${icon("close")}</button></div>${issues.length ? `<ul>${issues.map((issue) => `<li><button data-action="wf-select-issue" data-id="${esc(issue.node_id || "")}" data-edge="${esc(issue.edge_id || "")}">${esc(issue.message)}</button></li>`).join("")}</ul>` : '<p>节点、连线、变量和项目归属均已通过检查。</p>'}</div>`;
  }
  function runOverview() {
    const run = ws.run;
    if (!run) return '<p class="wf-muted">选择一条运行记录查看详情。</p>';
    return `${badge(run.state)}<p class="wf-muted">r${run.revision} · ${date(run.created_at)}</p>${run.error ? `<p class="wf-field-error">${esc(run.error.safe_message)}</p>` : ""}${run.summary_markdown ? `<div class="markdown">${markdown(run.summary_markdown)}</div>` : ""}<h3>节点进度</h3><div class="wf-run-node-list">${run.nodes.map((node) => `<button data-action="wf-select-node" data-id="${esc(node.node_id)}"><span>${esc(node.title)}</span>${nodeBadge(node.state)}</button>`).join("")}</div>`;
  }
  function runNode(node, execution) {
    const members = (execution.instances || []).map((member) => `<div class="wf-instance"><strong>#${member.ordinal + 1} ${esc(member.agent_id)}</strong> ${badge(member.state)}<button class="text-link" data-action="open-task-thread" data-id="${esc(member.task_id)}">进入对话</button>${member.state === "WAITING" ? `<button class="btn small" data-action="wf-resume-node" data-id="${esc(node.id)}" data-instance-index="${member.ordinal}">提供输入</button>` : ""}</div>`).join("");
    return `<h3>${esc(node.title || node.id)}</h3>${nodeBadge(execution.state)}${members}${execution.started_at ? `<p class="wf-muted">${date(execution.started_at)}${execution.ended_at ? ` · ${Math.max(0, execution.ended_at - execution.started_at)} ms` : ""}</p>` : ""}${execution.error ? `<p class="wf-field-error">${esc(execution.error.safe_message || execution.error.code)}</p>` : ""}${execution.summary_markdown ? `<div class="markdown wf-node-summary">${markdown(execution.summary_markdown)}</div>` : ""}${execution.state === "WAITING" && !execution.batch_id ? `<button class="btn small primary" data-action="wf-resume-node" data-id="${esc(node.id)}">提供输入并继续</button>` : ""}${execution.state === "UNCERTAIN" ? '<p class="wf-notice error">此节点需要核对停止证据与外部结果。可在对话的执行详情或 Codex 中核对后继续。</p>' : ""}<details class="wf-advanced"><summary>节点输入</summary>${content(execution.inputs)}</details><details class="wf-advanced"><summary>节点输出</summary>${content(execution.output)}</details>${execution.task_id ? `<button class="btn small" data-action="open-task-thread" data-id="${esc(execution.task_id)}">进入节点对话</button>` : ""}<button class="text-link" data-action="wf-run-overview">查看运行概览 ${icon("arrow")}</button>`;
  }
  function dialog() {
    if (!ws.dialog) return "";
    if (ws.dialog.type === "delete") return `<div class="wf-dialog-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="wf-dialog-title"><header><h2 id="wf-dialog-title">删除工作流？</h2><button class="icon-btn" data-action="wf-close-dialog" aria-label="关闭删除确认" ${ws.dialog.pending ? "disabled" : ""}>${icon("close")}</button></header><div class="modal-content"><p>“${esc(ws.dialog.name)}”将移入工作流回收站。</p><p class="form-hint">画布和运行记录保留，可从项目工作流右上角的回收站恢复。未完成或待核对的运行会阻止删除。</p>${ws.dialog.dirty ? '<p class="form-hint">当前未保存的修改将一并丢弃。</p>' : ""}</div><footer><button class="btn" id="wf-delete-cancel" data-action="wf-close-dialog" ${ws.dialog.pending ? "disabled" : ""}>取消</button><button class="btn danger" data-action="wf-confirm-delete" ${ws.dialog.pending ? "disabled" : ""}>${ws.dialog.pending ? "正在删除…" : "确认删除工作流"}</button></footer></section></div>`;
    const resume = ws.dialog.type === "resume";
    return `<div class="wf-dialog-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="wf-dialog-title"><header><h2 id="wf-dialog-title">${resume ? "继续等待的节点" : "运行工作流"}</h2><button class="icon-btn" data-action="wf-close-dialog" aria-label="关闭运行面板">${icon("close")}</button></header><div class="modal-content"><p>${resume ? "填写真实输入，继续当前节点。" : `${esc(ws.draft.name)} · r${ws.draft.revision}`}</p><div class="field"><span>${resume ? "补充信息" : "运行输入"}</span>${valueEditor(resume ? { input: JSON.parse(ws.dialog.input).input || "" } : JSON.parse(ws.dialog.input), "wf-run-inputs", resume ? { type: "object", properties: { input: { type: "string", title: "补充信息" } }, required: ["input"] } : ws.draft.graph.nodes.find(node => node.type === "start")?.config.input_schema)}</div>${!resume ? `<label class="field">保存到会话<select id="wf-run-session" data-focus="wf-run-session"><option value="">新建会话</option>${state.sessions.filter(session => session.project_id === ws.projectId && session.state === "OPEN").map(session => `<option value="${esc(session.id)}" ${ws.dialog.sessionId === session.id ? "selected" : ""}>${esc(session.title)}</option>`).join("")}</select></label><details class="wf-advanced"><summary>输入说明</summary>${schemaView(ws.draft.graph.nodes.find(node => node.type === "start")?.config.input_schema)}</details>` : ""}${ws.validation?.warnings.map((issue) => `<p class="form-hint">${esc(issue.message)}</p>`).join("") || ""}<p class="form-hint">${resume ? "保留当前 wait_token；已完成的节点不会重新执行。" : "运行保留固定版本；同会话中的各个运行独立管理。Agent 共享平台执行预算。"}</p></div><footer><button class="btn" data-action="wf-close-dialog">取消</button><button class="btn primary" data-action="${resume ? "wf-confirm-resume" : "wf-confirm-run"}">${icon("arrow")}${resume ? "继续执行" : "开始运行"}</button></footer></section></div>`;
  }

  async function save() {
    if (!ws.draft || !editable()) return;
    if (Object.values(ws.errors).some(Boolean)) throw new Error("请先修复节点参数中的 字段错误。");
    if (!ws.dirty) return ws.draft;
    const current = ws, draft = ws.draft, before = json(snapshot());
    try {
      const saved = await call("update", { workflow_id: draft.id, expected_revision: draft.revision,
        name: draft.name, description: draft.description, graph: draft.graph });
      current.items = current.items.map((item) => item.id === saved.id ? saved : item);
      if (ws === current && ws.draft?.id === saved.id) {
        const changedWhileSaving = json(snapshot()) !== before;
        if (!changedWhileSaving) { ws.draft = clone(saved); ws.dirty = false; ws.errors = {}; }
        else ws.draft.revision = saved.revision;
        ws.validation = changedWhileSaving ? null : saved.validation;
        ws.conflict = false;
        stash();
        render();
      } else {
        const buffer = current.buffers.get(saved.id);
        if (buffer) { buffer.draft.revision = saved.revision; buffer.dirty = true; }
      }
      return saved;
    } catch (error) {
      if (ws === current && ws.draft?.id === draft.id && error.code === "REVISION_CONFLICT") { ws.conflict = true; render(); }
      throw error;
    }
  }

  function seedGraph() {
    return { nodes: [
      { id: "start", type: "start", title: "开始", position: { x: 60, y: 160 }, config: { input_schema: { type: "object", properties: { input: { type: "string", title: "工作目标" } }, required: ["input"] } } },
      { id: "end", type: "end", title: "结束", position: { x: 420, y: 160 }, config: { outputs: { text: "{{inputs.input}}" } } },
    ], edges: [{ id: "start-end", source: "start", target: "end", source_handle: "default" }], viewport: { x: 0, y: 0, zoom: 1 } };
  }
  async function create(imported = null) {
    if (!ws.projectId) throw new Error("请先选择一个项目。");
    const current = ws;
    if (!ws.pendingCreate) ws.pendingCreate = { project_id: ws.projectId, name: imported?.name || "未命名工作流",
      description: imported?.description || "", graph: imported?.graph || seedGraph(), idempotency_key: idempotency() };
    let value;
    try { value = await call("create", ws.pendingCreate); }
    catch (error) { if (["INVALID_REQUEST", "PROJECT_MISMATCH", "PROJECT_CLOSED"].includes(error.code)) current.pendingCreate = null; throw error; }
    current.pendingCreate = null;
    if (current !== ws) return;
    const wasDeletedView = ws.deletedOnly;
    if (wasDeletedView) { ws.deletedOnly = false; ws.items = []; }
    if (!ws.items.some((item) => item.id === value.id)) ws.items.push(value);
    use(value);
    render();
    if (wasDeletedView) await load(ws.projectId, true);
    $("#wf-name")?.focus();
    $("#wf-name")?.select();
  }
  async function history(append = false) {
    const current = ws, identity = ws.draft.id;
    const page = await call("runs", { workflow_id: identity, order: "active", ...(append ? { cursor: ws.runCursor } : {}) });
    if (ws !== current || ws.draft?.id !== identity) return;
    ws.runs = orderedRuns(append ? [...new Map([...ws.runs, ...page.items].map(run => [run.id, run])).values()] : page.items);
    ws.runCursor = page.cursor;
    ws.runsMore = page.has_more;
    if (!ws.run && ws.runs.length) await selectRun(ws.runs[0].id);
    else render();
  }
  async function selectRun(identity) {
    const current = ws, workflowId = ws.draft.id, selection = ++ws.selectionEpoch;
    const run = await call("get_run", { run_id: identity });
    if (ws !== current || ws.draft?.id !== workflowId || selection !== ws.selectionEpoch) return;
    if (!state.session) await ensureSession?.(run.session_id);
    ws.run = run;
    ws.mode = "history";
    ws.selected = null;
    ws.edge = null;
    ws.runView = null;
    ws.needsFit = true;
    render();
  }
  async function refreshLive() {
    if (!ws.loaded || state.route !== "workflows") return;
    const current = ws, workflowId = ws.draft?.id, runId = ws.run?.id;
    const [activity, page, run] = await Promise.all([
      call("activity", { project_id: ws.projectId }),
      workflowId ? call("runs", { workflow_id: workflowId, order: "active" }) : null,
      runId ? call("get_run", { run_id: runId }) : null,
    ]);
    if (ws !== current) return;
    ws.activity = activity;
    if (ws.draft?.id === workflowId && page) {
      ws.runs = orderedRuns([...new Map([...ws.runs, ...page.items].map(item => [item.id, item])).values()]);
      if (!ws.run && ws.mode === "history" && ws.runs.length) await selectRun(ws.runs[0].id);
    }
    if (run && ws.run?.id === runId) ws.run = run;
    if (!drag && !ws.dialog) render();
  }
  function scheduleRunRefresh() {
    if (ws.timer) return;
    const current = ws;
    ws.timer = setTimeout(() => {
      current.timer = null;
      if (ws === current) refreshLive().catch(error => toast(error.message, true));
    }, 80);
  }
  function connect(source, target, handle) {
    if (!editable()) return;
    const error = canConnect(ws.draft.graph, source, target, handle);
    if (error) throw new Error(error);
    if (ws.draft.graph.edges.length >= 400) throw new Error("每个工作流最多 400 条连线。");
    remember();
    ws.draft.graph.edges.push({ id: "edge-" + idempotency(), source, target, source_handle: handle });
    ws.linking = null;
    ws.selected = target;
    ws.edge = null;
    dirty();
    render();
  }
  function nodeIdentity(kind) {
    let index = 1;
    while (ws.draft.graph.nodes.some((node) => node.id === `${kind}_${index}`)) index++;
    return `${kind}_${index}`;
  }
  function viewport() {
    return ws.mode === "history" && ws.run ? ws.runView || ws.run.graph.viewport : ws.draft?.graph.viewport;
  }
  function setViewport(value, changed = true) {
    if (ws.mode === "history") ws.runView = value;
    else if (ws.draft) { ws.draft.graph.viewport = value; if (changed) dirty(); }
    const world = $("#wf-world");
    if (world) world.style.transform = `translate(${value.x}px,${value.y}px) scale(${value.zoom})`;
    if ($("#wf-zoom-label")) $("#wf-zoom-label").textContent = Math.round(value.zoom * 100) + "%";
  }
  function fit(changed = true) {
    const canvas = $("#wf-canvas");
    if (canvas && graph()) setViewport(fitViewport(graph(), canvas.clientWidth, canvas.clientHeight), changed);
  }
  function zoom(factor, x = null, y = null) {
    const canvas = $("#wf-canvas"), view = viewport();
    if (!canvas || !view) return;
    const cx = x ?? canvas.clientWidth / 2, cy = y ?? canvas.clientHeight / 2;
    const scale = clampZoom(view.zoom * factor);
    setViewport({ x: cx - (cx - view.x) * scale / view.zoom, y: cy - (cy - view.y) * scale / view.zoom, zoom: scale });
  }
  function download() {
    const value = { format: "multi-agent.workflow", version: 1, name: ws.draft.name, description: ws.draft.description, graph: ws.draft.graph };
    const url = URL.createObjectURL(new Blob([json(value)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = (ws.draft.name.replace(/[<>:"/\\|?*]/g, "_") || "workflow") + ".json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function action(name, element) {
    const id = element.dataset.id;
    if (name === "wf-check-engine") return load(state.project?.id, true);
    if (name === "wf-new") return create();
    if (name === "wf-open") {
      const current = ws, value = await call("get", { workflow_id: id });
      if (ws === current && value.project_id === ws.projectId) { use(value); render(); if (ws.activity.some(item => item.workflow_id === id && item.active_count)) await history(); }
      return;
    }
    if (name === "wf-more") {
      const current = ws, page = await call("list", { project_id: ws.projectId, cursor: ws.cursor, include_archived: ws.archived, ...(ws.deletedOnly ? { deleted_only: true } : {}) });
      if (current !== ws) return;
      ws.items.push(...page.items.filter((item) => !ws.items.some((old) => old.id === item.id)));
      ws.cursor = page.cursor; ws.more = page.has_more; render(); return;
    }
    if (name === "wf-archived" || name === "wf-trash") {
      stash();
      if (name === "wf-trash") ws.deletedOnly = !ws.deletedOnly;
      else { ws.archived = !ws.archived; ws.deletedOnly = false; }
      clearEditor();
      return load(ws.projectId, true);
    }
    if (name === "wf-delete") {
      ws.dialog = { type: "delete", workflowId: ws.draft.id, name: ws.draft.name, dirty: ws.dirty };
      ws.dialogFocused = false; render(); return;
    }
    if (name === "wf-confirm-delete") {
      const current = ws, modal = ws.dialog;
      if (modal?.type !== "delete" || modal.pending) return;
      modal.pending = true; render();
      try {
        await call("delete", { workflow_id: modal.workflowId, confirmed: true });
        current.buffers.delete(modal.workflowId);
        current.pendingRuns.delete(modal.workflowId);
        current.items = current.items.filter((item) => item.id !== modal.workflowId);
        if (current.draft?.id === modal.workflowId) clearEditor(current);
        if (ws !== current) return;
        ws.dialog = null;
        await load(ws.projectId, true);
        toast("工作流已移入回收站");
      } catch (error) {
        if (error.code === "METHOD_NOT_FOUND") throw new Error("请重启平台服务并重连 Codex MCP，以加载工作流删除能力。");
        throw error;
      } finally {
        modal.pending = false;
        if (ws === current) render();
      }
      return;
    }
    if (name === "wf-restore") {
      const current = ws, identity = ws.draft.id;
      const value = await call("restore", { workflow_id: identity });
      current.buffers.delete(identity);
      if (current !== ws) return;
      ws.deletedOnly = false;
      clearEditor(); use(value);
      await load(ws.projectId, true);
      toast("工作流已恢复"); return;
    }
    if (name === "wf-save") { await save(); toast("工作流已保存"); return; }
    if (name === "wf-reload") {
      const current = ws, identity = ws.draft.id, saved = await call("get", { workflow_id: identity });
      if (ws !== current || ws.draft?.id !== identity) return;
      ws.backup = snapshot(); ws.buffers.delete(saved.id); ws.dirty = false; use(saved); render(); return;
    }
    if (name === "wf-export") { download(); return; }
    if (name === "wf-import") { $("#wf-import-file")?.click(); return; }
    if (name === "wf-archive") {
      if (ws.dirty && ws.mode !== "canvas") throw new Error("请先返回画布保存修改，再归档。");
      if (editable() && ws.dirty) await save();
      const saved = await call("archive", { workflow_id: ws.draft.id, archived: ws.draft.state !== "ARCHIVED" });
      ws.draft = clone(saved); ws.dirty = false; ws.buffers.delete(saved.id);
      return load(ws.projectId, true);
    }
    if (name === "wf-mode") {
      ws.mode = element.dataset.mode; ws.edge = null; ws.linking = null;
      if (ws.mode === "history") { ws.selected = null; render(); await history(); }
      else { ws.selected = ws.draft.graph.nodes[0]?.id; render(); }
      return;
    }
    if (name === "wf-validate" || name === "wf-run") {
      if (Object.values(ws.errors).some(Boolean)) throw new Error("请先修复节点参数中的 字段错误。");
      const current = ws, draftId = ws.draft.id;
      if (name === "wf-run") await save();
      if (current !== ws || ws.draft.id !== draftId) return;
      if (name === "wf-run" && ws.dirty) throw new Error("保存期间有新的修改，请保存后再运行。");
      const report = await call("validate", { project_id: ws.projectId, graph: ws.draft.graph });
      if (current !== ws || ws.draft.id !== draftId) return;
      ws.validation = report;
      if (name === "wf-run" && ws.validation.valid) {
        const schema = ws.draft.graph.nodes.find(node => node.type === "start")?.config.input_schema || {};
        const previous = JSON.parse(ws.runInput);
        const inputs = { ...schemaDefaults(schema), ...Object.fromEntries(Object.entries(previous).filter(([key]) => schema.additionalProperties !== false || key in (schema.properties || {}))) };
        const pending = ws.pendingRuns.get(draftId);
        ws.dialog = { type: "run", input: json(inputs), key: idempotency(), sessionId: pending ? pending.session_id || "" : state.session?.id || "" };
        ws.dialogFocused = false;
      }
      render(); return;
    }
    if (name === "wf-dismiss-validation") { ws.validation = null; render(); return; }
    if (name === "wf-select-issue") { ws.selected = id || null; ws.edge = element.dataset.edge || null; ws.inspector = true; render(); return; }
    if (name === "wf-close-dialog") { if (!ws.dialog?.pending) { ws.dialog = null; render(); } return; }
    if (name === "wf-confirm-run") {
      const inputs = readEditor(document.querySelector('[data-value-editor="wf-run-inputs"]'));
      ws.dialog.input = json(inputs);
      if (!inputs || typeof inputs !== "object" || Array.isArray(inputs)) throw new Error("运行输入须为 JSON 对象。");
      const current = ws, draftId = ws.draft.id, sessionId = $("#wf-run-session").value;
      if (!ws.pendingRuns.has(draftId)) ws.pendingRuns.set(draftId, { workflow_id: draftId, inputs, idempotency_key: ws.dialog.key, expected_revision: ws.draft.revision, ...(sessionId ? { session_id: sessionId } : {}) });
      const pending = ws.pendingRuns.get(draftId);
      if (JSON.stringify(pending.inputs) !== JSON.stringify(inputs) || (pending.session_id || "") !== sessionId) throw new Error("上一次运行的结果尚未核对，请先保留原输入重试或查看运行记录。");
      let run;
      try { run = await call("run", pending); }
      catch (error) {
        if (["INVALID_INPUT", "INVALID_WORKFLOW", "REVISION_CONFLICT", "PROJECT_CLOSED", "INVALID_STATE", "QUEUE_FULL", "INVALID_REQUEST", "PAYLOAD_TOO_LARGE"].includes(error.code)) current.pendingRuns.delete(draftId);
        throw error;
      }
      current.pendingRuns.delete(draftId);
      if (current !== ws || ws.draft.id !== draftId) return;
      ws.runInput = ws.dialog.input; ws.dialog = null; ws.mode = "history"; ws.run = null;
      if (!state.session) await ensureSession?.(run.session_id);
      await selectRun(run.id); await history(); await refreshLive(); return;
    }
    if (name === "wf-select-run") return selectRun(id);
    if (name === "wf-more-runs") return history(true);
    if (name === "wf-refresh-runs") { if (ws.run) await selectRun(ws.run.id); await history(); return; }
    if (name === "wf-cancel-run") { await call("cancel", { run_id: ws.run.id }); await selectRun(ws.run.id); return; }
    if (name === "wf-run-overview") { ws.selected = null; ws.edge = null; render(); return; }
    if (name === "wf-resume-node") {
      const current = ws, runId = ws.run.id;
      const node = ws.run.nodes.find((item) => item.node_id === id);
      const taskId = element.dataset.instanceIndex !== undefined ? node.instances[Number(element.dataset.instanceIndex)].task_id : node.task_id;
      const task = await api(`/tasks/${encodeURIComponent(taskId)}`);
      if (ws !== current || ws.run?.id !== runId) return;
      if (task.wait?.kind !== "input") throw new Error("此节点等待的是消息或其他任务，无需手动提供输入。");
      ws.dialog = { type: "resume", instanceIndex: element.dataset.instanceIndex === undefined ? null : Number(element.dataset.instanceIndex), nodeId: id, runId: ws.run.id, key: idempotency(), input: json({ wait_token: task.wait.wait_token, input: "" }) };
      ws.dialogFocused = false; render(); return;
    }
    if (name === "wf-confirm-resume") {
      const input = { ...JSON.parse(ws.dialog.input), ...readEditor(document.querySelector('[data-value-editor="wf-run-inputs"]')) };
      await call("resume_node", { run_id: ws.dialog.runId, node_id: ws.dialog.nodeId, input, idempotency_key: ws.dialog.key, instance_index: ws.dialog.instanceIndex });
      ws.dialog = null; await selectRun(ws.run.id); return;
    }
    if (name === "wf-toggle-inspector") { ws.inspector = !ws.inspector; render(); return; }
    if (name === "wf-select-node") { ws.selected = id; ws.edge = null; ws.inspector = true; render(); return; }
    if (name === "wf-select-edge") { ws.edge = id; ws.selected = null; ws.inspector = true; render(); return; }
    if (name === "wf-fit") { fit(); return; }
    if (name === "wf-zoom-in" || name === "wf-zoom-out") { zoom(name === "wf-zoom-in" ? 1.2 : 1 / 1.2); return; }
    if (!editable()) return;
    if (name === "wf-add-input" || name === "wf-remove-input") {
      const node = selected();
      if (node?.type !== "start") return;
      remember();
      const schema = node.config.input_schema;
      schema.properties ||= {};
      if (name === "wf-add-input") {
        let number = 1;
        while (schema.properties["input_" + number]) number++;
        schema.properties["input_" + number] = { type: "string", title: "新输入" };
      } else {
        delete schema.properties[element.dataset.key];
        schema.required = (schema.required || []).filter(key => key !== element.dataset.key);
      }
      dirty(); render(); return;
    }
    if (name === "wf-add-node" || name === "wf-duplicate") {
      const original = name === "wf-duplicate" ? selected() : null, kind = original?.type || element.dataset.kind;
      if (kind === "start" && ws.draft.graph.nodes.some((node) => node.type === "start")) return;
      if (ws.draft.graph.nodes.length >= 100) throw new Error("每个工作流最多 100 个节点。");
      remember();
      const canvas = $("#wf-canvas"), view = viewport(), id = nodeIdentity(kind);
      const node = { id, type: kind, title: original ? original.title + " 副本" : labels[kind],
        position: original ? { x: original.position.x + 40, y: original.position.y + 150 } : {
          x: Math.round((canvas.clientWidth / 2 - view.x) / view.zoom - 108), y: Math.round((canvas.clientHeight / 2 - view.y) / view.zoom - 60) },
        config: clone(original?.config || ws.types.find((type) => type.type === kind)?.defaults || {}) };
      ws.draft.graph.nodes.push(node); ws.selected = id; ws.edge = null; ws.inspector = true; dirty(); render(); return;
    }
    if (name === "wf-delete-node") {
      if (!ws.selected) return;
      remember();
      ws.draft.graph.nodes = ws.draft.graph.nodes.filter((node) => node.id !== ws.selected);
      ws.draft.graph.edges = ws.draft.graph.edges.filter((edge) => edge.source !== ws.selected && edge.target !== ws.selected);
      for (const key of Object.keys(ws.errors)) if (key.startsWith(ws.selected + ":")) delete ws.errors[key];
      ws.selected = null; ws.linking = null; dirty(); render(); return;
    }
    if (name === "wf-delete-edge") { remember(); ws.draft.graph.edges = ws.draft.graph.edges.filter((edge) => edge.id !== (id || ws.edge)); ws.edge = null; dirty(); render(); return; }
    if (name === "wf-source-port") { ws.linking = { source: id, handle: element.dataset.handle }; render(); return; }
    if (name === "wf-target-port") { if (ws.linking) connect(ws.linking.source, id, ws.linking.handle); return; }
    if (name === "wf-stop-link") { ws.linking = null; render(); return; }
    if (name === "wf-connect") { connect($("#wf-link-source").value, $("#wf-link-target").value, $("#wf-link-handle").value); return; }
    if (name === "wf-layout") { remember(); ws.draft.graph = autoLayout(ws.draft.graph); dirty(); render(); fit(); return; }
    if (name === "wf-undo" || name === "wf-redo") {
      const source = name === "wf-undo" ? ws.undo : ws.redo, target = name === "wf-undo" ? ws.redo : ws.undo;
      if (!source.length) return;
      target.push(snapshot()); const saved = source.pop();
      Object.assign(ws.draft, { graph: saved.graph, name: saved.name, description: saved.description });
      ws.errors = saved.errors; ws.selected = ws.draft.graph.nodes.some((node) => node.id === ws.selected) ? ws.selected : null;
      ws.edge = null; dirty(); render(); return;
    }
    if (name === "wf-insert-variable") {
      const node = selected();
      const fallback = node?.type === "template" ? "wf-config-template" : ["agent", "map"].includes(node?.type) ? "wf-payload-input" : node?.type === "end" ? "wf-outputs-text" : "wf-config-left";
      const field = document.querySelector(`[data-focus="${ws.lastField || fallback}"]`) || document.querySelector(`[data-focus="${fallback}"]`);
      if (!field || !["TEXTAREA", "INPUT"].includes(field.tagName)) { toast(`{{${element.dataset.ref}}}`); return; }
      field.focus(); field.setRangeText(`{{${element.dataset.ref}}}`, field.selectionStart ?? field.value.length, field.selectionEnd ?? field.value.length, "end");
      field.dispatchEvent(new Event("input", { bubbles: true })); return;
    }
  }

  function input(element) {
    const editor = element.closest("[data-value-editor]");
    const editorId = editor?.dataset.valueEditor;
    if (editorId === "wf-run-inputs" && ws.dialog) {
      try { ws.dialog.input = json({ ...JSON.parse(ws.dialog.input), ...readEditor(editor) }); editor.removeAttribute("data-invalid"); }
      catch (error) { editor.dataset.invalid = error.message; }
      return;
    }
    if (editorId?.startsWith("wf-") && editable() && selected()) {
      const field = editorId.slice(3), key = keyFor(selected(), field);
      remember();
      try { selected().config[field] = readEditor(editor); delete ws.errors[key]; }
      catch (error) { ws.errors[key] = error.message; }
      $("#wf-error-" + field).textContent = ws.errors[key] || "";
      dirty(); return;
    }
    if (!editable()) return;
    const node = selected();
    if (element.dataset.wfMeta) { remember(); ws.draft[element.dataset.wfMeta] = element.value; dirty(); return; }
    if (element.dataset.wfSchema && node?.type === "start") {
      const schema = node.config.input_schema, key = element.dataset.key, field = element.dataset.wfSchema;
      remember();
      if (field === "required") {
        schema.required = (schema.required || []).filter(name => name !== key);
        if (element.checked) schema.required.push(key);
      } else if (field === "name") {
        const name = element.value.trim();
        if (!name || (name !== key && Object.hasOwn(schema.properties, name))) {
          ws.errors[keyFor(node, "input_schema")] = "变量名称不能为空或重复。";
          element.setCustomValidity(ws.errors[keyFor(node, "input_schema")]); dirty(); return;
        }
        element.setCustomValidity("");
        delete ws.errors[keyFor(node, "input_schema")];
        schema.properties = Object.fromEntries(Object.entries(schema.properties).map(([old, value]) => [old === key ? name : old, value]));
        schema.required = (schema.required || []).map(old => old === key ? name : old);
        element.closest(".wf-input-field").querySelectorAll("[data-key]").forEach(item => { item.dataset.key = name; });
      } else schema.properties[key][field] = element.value;
      dirty(); return;
    }
    if (!element.dataset.wfField) return;
    remember();
    const field = element.dataset.wfField;
    if (field === "description") ws.draft.description = element.value;
    else if (!node) return;
    else if (field === "title") node.title = element.value;
    else if (["x", "y"].includes(field)) node.position[field] = Math.max(-100000, Math.min(100000, Number(element.value) || 0));
    else if (["attempt_timeout_s", "max_attempts", "max_concurrency"].includes(field)) node.config[field] = Number(element.value);
    else if (["left", "right"].includes(field)) {
      try { node.config[field] = JSON.parse(element.value); } catch { node.config[field] = element.value; }
    } else node.config[field] = element.value;
    dirty();
    updateGeometry();
  }
  async function change(element) {
    if (element.id === "wf-run-choice") return selectRun(element.value);
    if (element.id === "wf-run-session" && ws.dialog) { ws.dialog.sessionId = element.value; return; }
    if (element.id === "wf-import-file" && element.files?.length) {
      const file = element.files[0];
      if (file.size > 1024 * 1024) throw new Error("工作流文件不能超过 1 MiB。");
      let value;
      try { value = JSON.parse(await file.text()); } catch { throw new Error("请选择有效的工作流 JSON 文件。"); }
      if (value.format !== "multi-agent.workflow" || value.version !== 1 || !value.graph) throw new Error("工作流文件格式或版本不支持。");
      await create(value); return;
    }
    if (element.dataset.wfField === "operator" || element.dataset.wfField === "agent_id" || element.dataset.wfField === "retry_policy") render();
  }
  function updateGeometry() {
    for (const node of graph()?.nodes || []) {
      const element = [...document.querySelectorAll(".wf-node")].find((item) => item.dataset.nodeId === node.id);
      if (element) { element.style.left = node.position.x + "px"; element.style.top = node.position.y + "px"; }
    }
    if ($("#wf-edge-paths")) $("#wf-edge-paths").innerHTML = edges();
  }
  function mount() {
    const canvas = $("#wf-canvas");
    if (state.route !== "workflows" || !canvas) return;
    if (ws.needsFit) { ws.needsFit = false; fit(false); }
    else if (viewport()) setViewport(viewport(), false);
    updateGeometry();
    canvas.addEventListener("pointerdown", pointerDown);
    canvas.addEventListener("wheel", (event) => {
      if (!graph()) return;
      event.preventDefault();
      const box = canvas.getBoundingClientRect();
      zoom(event.deltaY < 0 ? 1.08 : 1 / 1.08, event.clientX - box.left, event.clientY - box.top);
    }, { passive: false });
    if (ws.dialog && !ws.dialogFocused) { ws.dialogFocused = true; $(ws.dialog.type === "delete" ? "#wf-delete-cancel" : '[data-value-editor="wf-run-inputs"] input,[data-value-editor="wf-run-inputs"] textarea')?.focus(); }
  }
  function pointerDown(event) {
    if (event.button !== 0 || !graph()) return;
    const canvas = event.currentTarget, port = event.target.closest(".wf-port.output"), head = event.target.closest(".wf-node-head");
    if (port && editable()) {
      event.preventDefault(); ws.linking = { source: port.dataset.id, handle: port.dataset.handle };
      drag = { type: "link", canvas, pointer: event.pointerId, project: ws.projectId };
    } else if (head && editable()) {
      event.preventDefault(); ws.selected = head.dataset.id; ws.edge = null; ws.inspector = true;
      const node = selected();
      $(".wf-edit-body")?.classList.add("inspector-open");
      document.querySelectorAll(".wf-node").forEach((element) => element.classList.toggle("selected", element.dataset.nodeId === node.id));
      if ($("#wf-inspector")) $("#wf-inspector").innerHTML = inspector();
      drag = { type: "node", canvas, pointer: event.pointerId, project: ws.projectId, node, x: event.clientX, y: event.clientY,
        initial: { ...node.position }, before: snapshot(), moved: false };
    } else if (!event.target.closest("button, .wf-node, .wf-edge-hit")) {
      event.preventDefault();
      drag = { type: "pan", canvas, pointer: event.pointerId, project: ws.projectId, x: event.clientX, y: event.clientY,
        initial: { ...viewport() }, moved: false };
      canvas.classList.add("panning");
    }
    if (drag) canvas.setPointerCapture(event.pointerId);
  }
  window.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointer !== event.pointerId || drag.project !== ws.projectId) return;
    const view = viewport();
    if (drag.type === "node") {
      if (!drag.moved && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 3) { ws.undo.push(drag.before); ws.redo = []; drag.moved = true; }
      drag.node.position.x = Math.max(-100000, Math.min(100000, Math.round(drag.initial.x + (event.clientX - drag.x) / view.zoom)));
      drag.node.position.y = Math.max(-100000, Math.min(100000, Math.round(drag.initial.y + (event.clientY - drag.y) / view.zoom)));
      updateGeometry();
    } else if (drag.type === "pan") {
      drag.moved = true;
      setViewport({ ...view, x: Math.max(-100000, Math.min(100000, drag.initial.x + event.clientX - drag.x)),
        y: Math.max(-100000, Math.min(100000, drag.initial.y + event.clientY - drag.y)) }, false);
    } else if (ws.linking) {
      const node = graph().nodes.find((item) => item.id === ws.linking.source), box = drag.canvas.getBoundingClientRect();
      const to = { x: (event.clientX - box.left - view.x) / view.zoom, y: (event.clientY - box.top - view.y) / view.zoom };
      $("#wf-preview")?.setAttribute("d", curve(portPoint(node, ws.linking.handle), to));
    }
  });
  function finishDrag(event, cancelled = false) {
    if (!drag || drag.pointer !== event.pointerId) return;
    const previous = drag; drag = null;
    previous.canvas.classList.remove("panning");
    if (previous.canvas.hasPointerCapture(event.pointerId)) previous.canvas.releasePointerCapture(event.pointerId);
    if (previous.project !== ws.projectId) return;
    if (previous.type === "link" && !cancelled) {
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".wf-port.input");
      if (target && ws.linking) {
        try { connect(ws.linking.source, target.dataset.id, ws.linking.handle); } catch (error) { toast(error.message, true); render(); }
      } else render();
    } else if (previous.moved) {
      if (ws.mode === "canvas") dirty();
      render();
    }
  }
  window.addEventListener("pointerup", (event) => finishDrag(event));
  window.addEventListener("pointercancel", (event) => finishDrag(event, true));
  document.addEventListener("focusin", (event) => {
    if (event.target.closest(".wf-inspector") && ["INPUT", "TEXTAREA"].includes(event.target.tagName)) ws.lastField = event.target.id;
  });
  window.addEventListener("keydown", (event) => {
    if (state.route !== "workflows" || state.modal) return;
    if (ws.dialog) {
      if (event.key === "Escape") { event.preventDefault(); if (!ws.dialog.pending) { ws.dialog = null; render(); } }
      if (event.key === "Tab") {
        const items = [...document.querySelectorAll(".wf-dialog-backdrop button, .wf-dialog-backdrop textarea")].filter((item) => !item.disabled);
        if (!items.length) { event.preventDefault(); return; }
        if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus(); }
        else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus(); }
      }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault(); save().then(() => toast("工作流已保存")).catch((error) => toast(error.message, true)); return;
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
    if (event.key === "Escape") { ws.linking = null; ws.selected = null; ws.edge = null; render(); return; }
    if (!editable()) return;
    let command;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") command = event.shiftKey ? "wf-redo" : "wf-undo";
    else if (["Delete", "Backspace"].includes(event.key)) command = ws.edge ? "wf-delete-edge" : "wf-delete-node";
    if (command) { event.preventDefault(); action(command, { dataset: {} }).catch((error) => toast(error.message, true)); }
    else if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key) && selected()) {
      event.preventDefault(); remember(); const node = selected(), step = event.shiftKey ? 10 : 1;
      node.position.x = Math.max(-100000, Math.min(100000, node.position.x + (event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0)));
      node.position.y = Math.max(-100000, Math.min(100000, node.position.y + (event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0)));
      dirty(); updateGeometry();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (ws.dirty || [...ws.buffers.values()].some((buffer) => buffer.dirty) || [...projects.values()].some((project) => project.dirty || [...project.buffers.values()].some((buffer) => buffer.dirty))) { event.preventDefault(); event.returnValue = ""; }
  });
  async function onEvent(event) {
    if (state.route === "workflows" && ws.loaded && event.payload?.project_id === ws.projectId && event.type.startsWith("workflow.") && event.payload?.workflow_run_id) {
      scheduleRunRefresh();
      return;
    }
    if (state.route !== "workflows" || !ws.loaded || event.payload?.project_id !== ws.projectId ||
        !["workflow.created", "workflow.updated", "workflow.archived", "workflow.deleted", "workflow.restored"].includes(event.type)) return;
    const current = ws, value = await call("get", { workflow_id: event.payload.workflow_id });
    if (ws !== current) return;
    const shown = ws.deletedOnly ? value.state === "DELETED" : value.state !== "DELETED" && (value.state === "ACTIVE" || ws.archived);
    if (!shown) ws.items = ws.items.filter((item) => item.id !== value.id);
    else if (ws.items.some((item) => item.id === value.id)) ws.items = ws.items.map((item) => item.id === value.id ? value : item);
    else ws.items.push(value);
    if (ws.draft?.id === value.id) {
      if (!ws.dirty && !drag) { ws.draft = clone(value); ws.errors = {}; }
      else if (value.revision > ws.draft.revision || value.state !== ws.draft.state) ws.conflict = true;
    }
    render();
  }
  return { page, load, action, input, change, mount, onEvent, refresh: refreshLive, isInteracting: () => !!drag };
}
