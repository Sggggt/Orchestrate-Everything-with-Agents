// Small dependency-free Markdown renderer. Raw HTML is always text.
const escape = (value) => String(value).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);

function safeLink(value) {
  if (/[\u0000-\u0020]/.test(value)) return null;
  try {
    const url = new URL(value, "http://local.invalid");
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? value : null;
  } catch { return null; }
}

function inline(value) {
  const tokens = [];
  const keep = html => "\u0000" + (tokens.push(html) - 1) + "\u0000";
  let text = String(value)
    .replace(/`([^`]+)`/g, (_, body) => keep("<code>" + escape(body) + "</code>"))
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
      const link = safeLink(url);
      return link ? keep('<a href="' + escape(link) + '" target="_blank" rel="noopener noreferrer">' +
        escape(label) + "</a>") : label;
    });
  text = escape(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return text.replace(/\u0000(\d+)\u0000/g, (_, n) => tokens[Number(n)] || "");
}
const cells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
const tableRule = line => line.includes("|") && cells(line).every(c => /^:?-{3,}:?$/.test(c));

export function markdown(value = "") {
  const lines = String(value).replace(/\r\n?/g, "\n").split("\n"), html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    const fence = line.match(/^\s*```([\w+-]*)\s*$/);
    if (fence) {
      const code = [];
      index++;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index++;
      html.push('<pre><code' + (fence[1] ? ' class="language-' + escape(fence[1]) + '"' : "") +
        ">" + escape(code.join("\n")) + "</code></pre>");
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      html.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
      index++; continue;
    }
    if (index + 1 < lines.length && line.includes("|") && tableRule(lines[index + 1])) {
      const header = cells(line), rows = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) rows.push(cells(lines[index++]));
      html.push('<div class="markdown-table"><table><thead><tr>' + header.map(c => "<th>" + inline(c) + "</th>").join("") +
        "</tr></thead><tbody>" + rows.map(r => "<tr>" + header.map((_, i) => "<td>" + inline(r[i] || "") + "</td>").join("") +
        "</tr>").join("") + "</tbody></table></div>");
      continue;
    }
    const list = line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
    if (list) {
      const tag = list[2] ? "ol" : "ul", items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
        if (!item || Boolean(item[2]) !== Boolean(list[2])) break;
        items.push("<li>" + inline(item[3]) + "</li>"); index++;
      }
      html.push("<" + tag + ">" + items.join("") + "</" + tag + ">"); continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      html.push("<blockquote>" + markdown(quote.join("\n")) + "</blockquote>"); continue;
    }
    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) { html.push("<hr>"); index++; continue; }
    const paragraph = [line]; index++;
    while (index < lines.length && lines[index].trim() &&
           !/^(?:#{1,6}\s|```|\s*[-+*>]\s|\s*\d+[.)]\s)/.test(lines[index]) &&
           !(index + 1 < lines.length && tableRule(lines[index + 1]))) paragraph.push(lines[index++]);
    html.push("<p>" + paragraph.map(inline).join("<br>") + "</p>");
  }
  return html.join("\n");
}
