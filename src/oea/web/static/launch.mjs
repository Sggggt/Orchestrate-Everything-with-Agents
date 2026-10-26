const fileName = value => String(value || "").split(/[\\/]/).at(-1);
const modes = { "python-script": "Python 脚本", "python-module": "Python 模块", "node-script": "Node.js 脚本", program: "直接运行程序" };
const pythonMode = mode => mode.startsWith("python-");

export function argumentOptions(args) {
  const rows = [];
  let positional = false;
  for (let i = 0; i < args.length; i++) {
    const token = args[i];
    if (token === "--") positional = true;
    const match = !positional && token.match(/^(--?[^\s=]+)(?:=(.*))?$/s);
    if (!match || /^-\d/.test(token)) rows.push({ kind: "argument", value: token, enabled: true });
    else if (match[2] !== undefined) rows.push({ kind: "option", name: match[1], value: match[2], joined: true, enabled: true });
    else if (i + 1 < args.length && (!args[i + 1].startsWith("-") || /^-\d/.test(args[i + 1]))) {
      rows.push({ kind: "option", name: token, value: args[++i], joined: false, enabled: true });
    } else rows.push({ kind: "switch", name: token, enabled: true });
  }
  return rows;
}

export function describeLaunch(command, defaultMode = "python-script") {
  const argv = [...command], program = argv[0] || (defaultMode === "python-script" ? "python" : "");
  const base = fileName(program).toLowerCase();
  let mode = "program", entry = "", index = 1, flags = [];
  if (/^(python(?:\d+(?:\.\d+)*)?w?|py)(?:\.exe)?$/.test(base)) {
    while (["-B", "-u"].includes(argv[index])) flags.push(argv[index++]);
    if (argv[index] === "-m" && argv[index + 1]) { mode = "python-module"; entry = argv[index + 1]; index += 2; }
    else if (/\.pyw?$/i.test(argv[index] || "")) { mode = "python-script"; entry = argv[index++]; }
  } else if (/^(node|nodejs)(?:\.exe)?$/.test(base) && /\.[cm]?[jt]s$/i.test(argv[1] || "")) {
    mode = "node-script"; entry = argv[1]; index = 2;
  }
  if (!argv.length) { mode = defaultMode; flags = pythonMode(mode) ? ["-B"] : []; }
  if (mode === "program") { index = 1; flags = []; }
  return { mode, program, entry, flags, options: argumentOptions(argv.slice(index)) };
}

export function buildLaunch(model) {
  if (!model.program.trim()) throw new Error("请填写用于启动的程序。");
  if (model.mode !== "program" && !model.entry.trim()) throw new Error(model.mode === "python-module" ? "请填写 Agent 模块名称。" : "请填写需要运行的入口文件。");
  const args = [];
  if (pythonMode(model.mode)) args.push(...model.flags);
  if (model.mode === "python-module") args.push("-m");
  if (model.mode !== "program") args.push(model.entry);
  for (const row of model.options) {
    if (!row.enabled) continue;
    if (row.kind === "argument") args.push(row.value);
    else {
      if (!/^--?[^\s=]+$/.test(row.name)) throw new Error("选项名称应以 - 或 -- 开头，且不能包含空格。");
      if (row.kind === "switch") args.push(row.name);
      else if (row.joined) args.push(row.name + "=" + row.value);
      else args.push(row.name, row.value);
    }
  }
  return [model.program, ...args];
}

// A view of the existing argv contract. It neither executes code nor changes saved settings.
export function createLaunchEditor({ scope, getDraft, read, write, escape: esc, icon, render, purpose, defaultMode = "python-script" }) {
  const views = new WeakMap();
  function view(draft) {
    if (!views.has(draft)) {
      const original = [...read(draft)];
      views.set(draft, { original, model: describeLaunch(original, defaultMode), editing: !original.length, customProgram: false, dirty: false, advanced: false, locations: false });
    }
    return views.get(draft);
  }
  const field = (label, name, value, hint = "", required = false) => `<label class="field">${label}<input data-launch-field="${name}" data-focus="${scope}-launch-${name}" value="${esc(value)}" ${required ? "required" : ""}>${hint ? `<small>${hint}</small>` : ""}</label>`;
  function optionRows(model, editable) {
    return model.options.map((row, index) => editable ? `<div class="launch-option"><label class="check-row"><input type="checkbox" data-launch-option="enabled" data-index="${index}" ${row.enabled ? "checked" : ""}>使用此${row.kind === "argument" ? "参数" : "选项"}</label>${row.kind !== "argument" ? `<label class="field">${row.kind === "switch" ? "开关名称" : "选项名称"}<input data-launch-option="name" data-index="${index}" data-focus="${scope}-option-${index}-name" value="${esc(row.name)}" placeholder="例如 --mode"></label>` : ""}${row.kind !== "switch" ? `<label class="field">${row.kind === "argument" ? "附加参数" : "设置值"}<input data-launch-option="value" data-index="${index}" data-focus="${scope}-option-${index}-value" value="${esc(row.value)}"></label>` : ""}<button type="button" class="text-link danger-text" data-action="launch-remove-option" data-index="${index}">移除此项</button></div>` : `<div><dt>${esc(row.kind === "argument" ? "附加参数" : row.name)}</dt><dd>${row.kind === "switch" ? "已开启" : esc(row.value || "（空值）")}</dd></div>`).join("");
  }
  function panel(draft = getDraft()) {
    const current = view(draft), model = current.editing ? current.model : describeLaunch(read(draft), defaultMode);
    const configured = read(draft).length > 0;
    const options = model.options.filter(row => row.enabled);
    const flags = [model.flags.includes("-B") ? "不生成 Python 缓存" : "", model.flags.includes("-u") ? "及时传回输出" : ""].filter(Boolean);
    const summary = `<dl class="launch-summary"><div><dt>启动方式</dt><dd>${modes[model.mode]}</dd></div><div><dt>${model.mode === "python-module" ? "Agent 模块" : model.mode === "program" ? "运行程序" : "入口文件"}</dt><dd>${esc(model.mode === "program" ? fileName(model.program) : model.mode === "python-module" ? model.entry : fileName(model.entry))}</dd></div>${flags.length ? `<div><dt>Python 设置</dt><dd>${flags.join(" · ")}</dd></div>` : ""}${options.length ? `<div><dt>附加选项</dt><dd>${options.length} 项，由程序定义</dd></div>` : ""}</dl>`;
    if (!current.editing) return `<section class="form-section launch-settings" data-launch-scope="${scope}"><div class="section-heading"><h3>${scope === "agent" ? "本机运行方式" : "本地服务启动方式"}</h3><button type="button" class="btn small" data-action="launch-edit">${icon("settings")}修改启动设置</button></div><p class="form-hint">${purpose}</p>${summary}${current.dirty ? `<p class="launch-unsaved" role="status">启动设置已修改，点击“${scope === "agent" ? "保存配置" : "保存服务"}”后生效。</p>` : ""}<details class="form-advanced launch-locations" data-launch-disclosure="locations" ${current.locations ? "open" : ""}><summary>查看完整位置与选项</summary><dl class="launch-summary"><div><dt>运行程序</dt><dd>${esc(model.program)}</dd></div>${model.entry ? `<div><dt>${model.mode === "python-module" ? "模块名称" : "入口文件"}</dt><dd>${esc(model.entry)}</dd></div>` : ""}${optionRows(model, false)}</dl></details></section>`;
    const programLabel = pythonMode(model.mode) ? "Python 运行程序" : model.mode === "node-script" ? "Node.js 运行程序" : "运行程序";
    const defaultProgram = pythonMode(model.mode) ? "python" : model.mode === "node-script" ? "node" : "";
    return `<section class="form-section launch-settings" data-launch-scope="${scope}"><h3>${scope === "agent" ? "设置 Agent 的启动方式" : "设置本地服务的启动方式"}</h3><p class="form-hint">${purpose}</p><div class="form-grid"><label class="field">启动方式<select data-launch-field="mode" data-focus="${scope}-launch-mode">${Object.entries(modes).map(([mode, name]) => `<option value="${mode}" ${model.mode === mode ? "selected" : ""}>${name}</option>`).join("")}</select><small>更换方式后重新填写入口与选项；取消修改可恢复原设置。</small></label><label class="field">${programLabel}<select data-launch-field="program-choice" data-focus="${scope}-launch-program-choice"><option value="current" ${!current.customProgram ? "selected" : ""}>${model.program ? (configured ? "沿用 " : "使用 ") + esc(fileName(model.program)) : "选择程序"}</option>${defaultProgram ? `<option value="default">使用本机默认 ${defaultProgram === "python" ? "Python" : "Node.js"}</option>` : ""}<option value="custom" ${current.customProgram ? "selected" : ""}>指定其他程序</option></select><small>由这个程序启动入口文件。</small></label></div>${current.customProgram || !model.program ? field(programLabel + "位置", "program", model.program, "填写本机程序的完整路径，或已可用的程序名称。", true) : ""}${model.mode !== "program" ? field(model.mode === "python-module" ? "Agent 模块名称" : "需要运行的入口文件", "entry", model.entry, model.mode === "python-module" ? "例如 my_agent.main，由所选 Python 环境加载。" : scope === "agent" ? "填写开发者提供的 Agent 入口文件，程序需能接收平台任务并回传结果。" : "填写 MCP 服务脚本的本机路径。", true) : '<p class="form-hint">平台直接启动所选程序，所需选项在高级设置中配置。</p>'}<details class="form-advanced" data-launch-disclosure="advanced" ${current.advanced ? "open" : ""}><summary>其他启动选项（高级） · ${options.length} 项</summary><p class="form-hint">通常保留已有选项。它们由执行程序定义；任务目标在对话或工作流中填写。</p>${pythonMode(model.mode) ? `<label class="check-row"><input type="checkbox" data-launch-flag="-B" ${model.flags.includes("-B") ? "checked" : ""}>不生成 Python 缓存文件</label><label class="check-row"><input type="checkbox" data-launch-flag="-u" ${model.flags.includes("-u") ? "checked" : ""}>及时传回输出</label>` : ""}<div class="launch-options">${optionRows(model, true)}</div><div class="launch-option-add"><label class="field">添加哪类选项<select data-launch-new-kind><option value="option">带设置值的选项</option><option value="switch">开启或关闭的开关</option><option value="argument">按顺序传入的附加参数</option></select></label><button type="button" class="btn small" data-action="launch-add-option">${icon("plus")}添加选项</button></div></details><div class="launch-actions"><button type="button" class="btn" data-action="launch-apply">完成修改</button>${configured ? '<button type="button" class="text-link" data-action="launch-cancel">取消修改</button>' : ""}<span class="form-hint">最后点击“${scope === "agent" ? "保存配置" : "保存服务"}”生效。</span></div></section>`;
  }
  function commit(draft = getDraft()) {
    const current = view(draft);
    if (!current.editing) return read(draft);
    const command = buildLaunch(current.model);
    write(draft, command);
    current.dirty = command.length !== current.original.length || command.some((value, index) => value !== current.original[index]);
    current.editing = false;
    return command;
  }
  function action(name, element) {
    if (!name.startsWith("launch-") || element.closest("[data-launch-scope]")?.dataset.launchScope !== scope) return false;
    const current = view(getDraft());
    if (name === "launch-edit") { current.model = describeLaunch(read(getDraft()), defaultMode); current.editing = true; current.customProgram = false; }
    if (name === "launch-cancel") { current.model = describeLaunch(read(getDraft()), defaultMode); current.editing = false; }
    if (name === "launch-apply") commit();
    if (name === "launch-remove-option") current.model.options.splice(Number(element.dataset.index), 1);
    if (name === "launch-add-option") {
      current.advanced = true;
      const kind = element.closest("[data-launch-scope]").querySelector("[data-launch-new-kind]").value;
      current.model.options.push({ kind, name: "", value: "", joined: false, enabled: true });
    }
    render();
    return true;
  }
  function input(element) {
    if (element.closest("[data-launch-scope]")?.dataset.launchScope !== scope) return;
    const current = view(getDraft()), model = current.model, field = element.dataset.launchField;
    if (element.dataset.launchOption) model.options[Number(element.dataset.index)][element.dataset.launchOption] = element.type === "checkbox" ? element.checked : element.value;
    if (element.dataset.launchFlag) {
      const flag = element.dataset.launchFlag;
      if (!element.checked) model.flags = model.flags.filter(value => value !== flag);
      else if (!model.flags.includes(flag)) model.flags.push(flag);
    }
    if (field === "entry" || field === "program") model[field] = element.value;
    if (field === "mode" && element.value !== model.mode) {
      model.mode = element.value; model.entry = ""; model.flags = pythonMode(model.mode) ? ["-B"] : [];
      model.options = [];
      model.program = pythonMode(model.mode) ? "python" : model.mode === "node-script" ? "node" : "";
      current.customProgram = false;
    }
  }
  function change(element) {
    if (element.closest("[data-launch-scope]")?.dataset.launchScope !== scope) return;
    const current = view(getDraft());
    if (element.dataset.launchField === "program-choice") {
      current.customProgram = element.value === "custom";
      if (element.value === "default") current.model.program = pythonMode(current.model.mode) ? "python" : "node";
      render();
    } else if (element.dataset.launchField === "mode") render();
  }
  function toggle(element) {
    if (!element.isConnected || element.closest("[data-launch-scope]")?.dataset.launchScope !== scope) return;
    if (element.dataset.launchDisclosure) view(getDraft())[element.dataset.launchDisclosure] = element.open;
  }
  return { panel, action, input, change, toggle, commit };
}
