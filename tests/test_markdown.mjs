import assert from "node:assert/strict";
import test from "node:test";
import { markdown } from "../src/ma_runtime/web/static/markdown.mjs";

test("headings, paragraphs, lists, table and links render as Markdown", () => {
  const html = markdown("# 摘要\n\n**完成**，见[报告](https://example.com).\n\n- 已检查\n- 待确认\n\n|项目|状态|\n|---|---|\n|示例项目|待审|");
  for (const tag of ["<h1>", "<strong>", "<ul>", "<li>", "<table>", "<th>", '<a href="https://example.com"'])
    assert.ok(html.includes(tag), tag);
});
test("HTML and script URLs are never executable", () => {
  const html = markdown('<img src=x onerror=alert(1)>\n\n[x](javascript:alert)\n\n[y](data:text/html,evil)');
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(!html.includes('href="data:'));
});
test("code fences, inline code and partial streamed Markdown are supported", () => {
  assert.ok(markdown("`a<b`").includes("<code>a&lt;b</code>"));
  const html = markdown("```sql\nselect 1 < 2;");
  assert.ok(html.includes('<pre><code class="language-sql">select 1 &lt; 2;</code></pre>'));
  assert.doesNotThrow(() => markdown("### 任务\n\n**进行"));
});
