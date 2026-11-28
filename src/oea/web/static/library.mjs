import { $, element, escape, icon, modal, notify } from "./ui.mjs";

export const pluginName = item => item.id === "plugin-creator" ? "插件创建器" : item.id === "agent-factory" ? "Agent 工厂" : item.name;

const tones = ["iris", "jade", "ochre", "ocean", "rose"];
export function glyph(name, symbol, tone) {
  const hash = Array.from(String(name)).reduce((sum, char) => sum + char.codePointAt(0), 0);
  return `<span class="library-glyph pigment-${tone || tones[hash % tones.length]}" aria-hidden="true">${icon(symbol)}</span>`;
}

export function actionSheet(title, actions) {
  const content = element('<div class="library-actions"></div>');
  const close = modal(title, content);
  for (const {label, symbol, run, disabled, danger} of actions) {
    const button = element(`<button type="button" class="library-action${danger ? " danger-text" : ""}" ${disabled ? "disabled" : ""}>${icon(symbol)}<span>${escape(label)}</span></button>`);
    button.onclick = async () => { await close(); try { await run(); } catch(error) { notify(error.message,true); } };
    content.append(button);
  }
  return close;
}

export function searchControl(label) {
  return `<label class="library-search">${icon("search")}<input type="search" data-library-search placeholder="${escape(label)}" aria-label="${escape(label)}"></label>`;
}

export function libraryRow({name, symbol, tone, index, state, active, openLabel}) {
  return `<article class="library-row" data-row="${index}"><button type="button" class="library-entry" data-open title="${escape(openLabel || name)}">
    ${glyph(name, symbol, tone)}<strong>${escape(name)}</strong></button>
    <span class="library-indicator ${active ? "is-ready" : ""}" role="img" title="${escape(state)}" aria-label="${escape(state)}"></span>
    <button type="button" class="icon-btn" data-options title="${escape(name)} · 更多操作" aria-label="${escape(name)} · 更多操作">${icon("more")}</button></article>`;
}

export function bindSearch(root, filter) {
  const input = $("[data-library-search]", root);
  input.oninput = () => filter(input.value.trim().toLocaleLowerCase());
}
