import { icon } from "./ui.mjs";
import { markdown } from "./markdown.mjs";

const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const typeOf = value => value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
const names = { string: "文本", number: "数字", integer: "整数", boolean: "开关", object: "字段组", array: "列表", null: "空值" };

export function compactTitle(value) {
  return String(value).trim().split(/\r?\n/, 1)[0]
    .replace(/^#{1,6}\s+/, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/(\*\*|__|~~|`)(.+?)\1/g, "$2")
    .replace(/\*([^*]+)\*/g, "$1").slice(0, 80);
}

// Content is rendered at the display boundary; the underlying result stays intact.
export function content(value) {
  if (typeof value === "string") {
    if (/^\s*[\[{]/.test(value)) {
      try { return content(JSON.parse(value)); } catch { /* Ordinary prose may begin with a bracket. */ }
    }
    return `<div class="markdown">${markdown(value)}</div>`;
  }
  if (value == null) return '<span class="value-empty">未提供</span>';
  if (Array.isArray(value)) return value.length ? `<ol class="value-list">${value.map(item => `<li>${content(item)}</li>`).join("")}</ol>` : '<span class="value-empty">暂无项目</span>';
  if (typeof value === "object") return Object.keys(value).length ? `<dl class="value-fields">${Object.entries(value).map(([key, item]) => `<div><dt>${escape(key)}</dt><dd>${content(item)}</dd></div>`).join("")}</dl>` : '<span class="value-empty">暂无字段</span>';
  return `<span>${typeof value === "boolean" ? (value ? "是" : "否") : escape(value)}</span>`;
}

export function schemaView(schema = {}) {
  const required = schema.required || [];
  return `<div class="schema-fields">${Object.entries(schema.properties || {}).map(([key, field]) => `<div><strong>${escape(field.title || key)}</strong><code>${escape(key)}</code><span>${escape(names[field.type] || field.type || "任意类型")}${required.includes(key) ? " · 必填" : " · 可选"}</span>${field.description ? content(field.description) : ""}${field.enum ? `<small>可选值：${field.enum.map(escape).join("、")}</small>` : ""}${field.properties ? schemaView(field) : ""}</div>`).join("") || '<p class="form-hint">未声明固定参数，可按服务协议添加字段。</p>'}</div>`;
}

export function toolResult(result) {
  if (result.structured_content != null) return content(result.structured_content);
  return (result.content || []).map(block => block.type === "text" ? content(block.text) : block.type === "image" && /^image\/(png|jpeg|webp|gif)$/.test(block.mimeType) ? `<img class="result-image" src="data:${block.mimeType};base64,${escape(block.data)}" alt="工具返回的图片">` : content(block)).join("") || content(result);
}

export function schemaDefaults(schema = {}) {
  if (schema.default !== undefined) return schema.default;
  if (schema.enum) return schema.enum[0];
  if (schema.type === "object" || schema.properties) return Object.fromEntries((schema.required || []).map(key => [key, schemaDefaults(schema.properties?.[key] || {})]));
  if (schema.type === "array") return [];
  if (["number", "integer"].includes(schema.type)) return schema.minimum ?? 0;
  return schema.type === "boolean" ? false : "";
}

// A small value editor, not a second schema validator. Protocol validation stays on the server.
export function valueEditor(value, id, schema = null) {
  return `<div class="value-editor" data-value-editor="${escape(id)}">${editorNode(value, schema, id)}</div>`;
}
function editorNode(value, schema, focus) {
  const declaredType = typeof schema?.type === "string" ? schema.type : null;
  const type = declaredType || typeOf(value), group = ["array", "object"].includes(type);
  let body;
  if (group) {
    const entries = type === "array" ? value.map((item, index) => [String(index), item]) : Object.entries(value || {});
    body = `<div class="value-entries">${entries.map(([key, item]) => entry(key, item, schema?.properties?.[key] || (type === "array" ? schema?.items : schema?.additionalProperties), focus + "-" + key, type, schema?.required?.includes(key), Boolean(schema?.properties?.[key]))).join("")}</div>${schema?.properties ? `<select data-value-choice aria-label="选择要添加的参数"><option value="">选择参数</option>${Object.entries(schema.properties).map(([key, field]) => `<option value="${escape(JSON.stringify({ key, field, required: schema.required?.includes(key) }))}">${escape(field.title || key)}</option>`).join("")}</select>` : ""}<button type="button" class="text-link" data-value-add>＋ ${type === "array" ? "添加一项" : "添加字段"}</button>`;
  } else if (schema?.enum) body = `<select data-value-input data-focus="${escape(focus)}">${schema.enum.map(item => `<option value="${escape(JSON.stringify(item))}" ${item === value ? "selected" : ""}>${escape(item)}</option>`).join("")}</select>`;
  else if (type === "boolean") body = `<label class="check-row"><input data-value-input data-focus="${escape(focus)}" type="checkbox" ${value ? "checked" : ""}>启用</label>`;
  else if (type === "null") body = '<span class="value-empty">空值</span>';
  else if (schema?.writeOnly) body = `<input type="password" data-value-input data-focus="${escape(focus)}" aria-label="敏感值" autocomplete="new-password" value="${escape(value)}">`;
  else body = `<${type === "string" ? "textarea" : "input"} data-value-input data-focus="${escape(focus)}" aria-label="${escape(schema?.title || "字段内容")}" ${type === "string" ? 'rows="2"' : `type="number" step="${type === "integer" ? "1" : "any"}" value="${escape(value)}"`} ${schema?.minimum !== undefined ? `min="${escape(schema.minimum)}"` : ""} ${schema?.maximum !== undefined ? `max="${escape(schema.maximum)}"` : ""}>${type === "string" ? escape(value) + "</textarea>" : ""}`;
  const childSchema = schema?.items || (typeof schema?.additionalProperties === "object" ? schema.additionalProperties : null);
  return `<div class="value-node" data-value-type="${escape(type)}" data-value-focus="${escape(focus)}" ${childSchema ? `data-value-child-schema="${escape(JSON.stringify(childSchema))}"` : ""} ${schema?.enum ? 'data-value-enum="true"' : ""}><div class="value-node-toolbar">${declaredType || schema?.enum ? `<span>${escape(names[type] || type)}</span>` : `<select data-value-kind aria-label="字段类型">${Object.entries(names).filter(([key]) => key !== "integer").map(([key, label]) => `<option value="${key}" ${key === type ? "selected" : ""}>${label}</option>`).join("")}</select>`}</div>${body}</div>`;
}
function entry(key, value, schema, focus, parent, required = false, fixed = false) {
  return `<div class="value-entry">${parent === "object" ? `<label class="value-key">${schema?.title ? `<span>${escape(schema.title)}</span>` : ""}<input data-value-key aria-label="字段名称" data-focus="${escape(focus)}-key" value="${escape(key)}" ${fixed ? "readonly" : ""} required>${required ? '<small>必填</small>' : ""}</label>` : '<span class="value-item-label">列表项</span>'}${schema?.description ? `<div class="form-hint">${content(schema.description)}</div>` : ""}${editorNode(value, schema, focus)}${required ? "" : '<button type="button" class="icon-btn danger-text" data-value-remove title="移除此字段" aria-label="移除此字段">${icon("trash")}</button>'}</div>`;
}
export function readEditor(root) {
  const node = root.matches(".value-node") ? root : root.querySelector(":scope > .value-node");
  const type = node.dataset.valueType;
  if (["object", "array"].includes(type)) {
    const entries = [...node.querySelector(":scope > .value-entries").children];
    if (type === "array") return entries.map(item => readEditor(item));
    const pairs = entries.map(item => [item.querySelector(":scope > .value-key input").value, readEditor(item)]);
    const keys = pairs.map(([key]) => key);
    if (keys.some(key => !key.trim()) || new Set(keys).size !== keys.length) throw new Error("字段名称不能为空或重复。");
    return Object.fromEntries(pairs);
  }
  const input = node.querySelector("[data-value-input]");
  if (node.dataset.valueEnum) return JSON.parse(input.value);
  if (type === "null") return null;
  if (type === "boolean") return input.checked;
  if (["number", "integer"].includes(type)) {
    if (!input.value.trim() || !input.checkValidity()) throw new Error("请填写有效数字。");
    return Number(input.value);
  }
  return input.value;
}
export function editorInteraction(event) {
  const target = event.target, root = target.closest("[data-value-editor]");
  if (!root) return false;
  let changed = false;
  if (event.type === "click") {
    if (target.closest("[data-value-add]")) {
      const node = target.closest(".value-node"), list = node.querySelector(":scope > .value-entries");
      const choice = node.querySelector(":scope > [data-value-choice]");
      if (choice && !choice.value) return true;
      const selected = choice ? JSON.parse(choice.value) : null;
      const key = selected?.key ?? (node.dataset.valueType === "array" ? String(list.children.length) : `field_${list.children.length + 1}`);
      if ([...list.querySelectorAll(":scope > .value-entry > .value-key input")].some(input => input.value === key)) return true;
      const field = selected?.field || (node.dataset.valueChildSchema ? JSON.parse(node.dataset.valueChildSchema) : null);
      list.insertAdjacentHTML("beforeend", entry(key, field ? schemaDefaults(field) : "", field, node.dataset.valueFocus + "-" + key, node.dataset.valueType, selected?.required, Boolean(selected)));
      list.lastElementChild.querySelector("input,textarea")?.focus(); changed = true;
    } else if (target.closest("[data-value-remove]")) { target.closest(".value-entry").remove(); changed = true; }
    else return false;
    event.preventDefault();
  } else if (event.type === "change" && target.matches("[data-value-kind]")) {
    const node = target.closest(".value-node"), type = target.value;
    node.outerHTML = editorNode(({ object: {}, array: [], boolean: false, number: 0, null: null, string: "" })[type], null, node.dataset.valueFocus); changed = true;
  }
  if (changed) root.dispatchEvent(new Event("input", { bubbles: true }));
  return changed;
}
