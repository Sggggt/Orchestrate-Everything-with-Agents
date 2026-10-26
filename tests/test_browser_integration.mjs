import assert from "node:assert/strict";
import test from "node:test";
import { content, compactTitle, schemaView, schemaDefaults, valueEditor, toolResult } from "../src/oea/web/static/presentation.mjs";

test("compact task labels keep the first line without Markdown source markers", () => {
  assert.equal(compactTitle("# **Result**\n\n- Full details stay in the task"), "Result");
  assert.equal(compactTitle("\n\n# Result\n\nDetails"), "Result");
  assert.equal(compactTitle("Review `input_value` and [reference](https://example.com)"), "Review input_value and reference");
  assert.equal(compactTitle("input_value_name"), "input_value_name");
});

test("structured results render nested fields and Markdown without executing content", () => {
  const html = content({ conclusion: "## Ready\n\n**Checked**", items: [true, 2, null], source: '<img src=x onerror="alert(1)">' });
  assert.ok(html.includes("<h2>Ready</h2>"));
  assert.ok(html.includes("<strong>Checked</strong>"));
  assert.ok(html.includes("<dt>items</dt>"));
  assert.ok(html.includes("&lt;img"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(content('{"status":"**Done**"}').includes("<strong>Done</strong>"));
  assert.ok(content("[ordinary prose]").includes("[ordinary prose]"));
});

test("schema forms retain required types, optional choices and secret masking", () => {
  const schema = { type: "object", properties: {
    name: { type: "string", title: "Name" }, count: { type: "integer", minimum: 1 },
    enabled: { type: "boolean" }, mode: { type: "string", enum: ["first", "second"] },
    nested: { type: "object", properties: { value: { type: "number" } }, required: ["value"] },
  }, required: ["name", "count", "enabled", "nested"] };
  const defaults = schemaDefaults(schema);
  assert.deepEqual(defaults, { name: "", count: 1, enabled: false, nested: { value: 0 } });
  const html = valueEditor(defaults, "example", schema);
  assert.ok(html.includes('type="number" step="1"'));
  assert.ok(html.includes('type="checkbox"'));
  assert.ok(html.includes("选择参数"));
  assert.ok(schemaView(schema).includes("必填"));
  assert.ok(valueEditor({ key: "value" }, "secret", { type: "object", additionalProperties: { type: "string", writeOnly: true } }).includes('type="password"'));
  assert.ok(!valueEditor(1, "safe", { type: "number", minimum: '\" onfocus=\"alert(1)' }).includes('min="" onfocus'));
});

test("MCP results prefer structured fields and render text blocks", () => {
  assert.ok(toolResult({ structured_content: { text: "**Result**" }, content: [{ type: "text", text: "duplicate" }] }).includes("<strong>Result</strong>"));
  assert.ok(!toolResult({ structured_content: { text: "Done" }, content: [{ type: "text", text: "duplicate" }] }).includes("duplicate"));
  assert.ok(toolResult({ content: [{ type: "text", text: "# Result" }] }).includes("<h1>Result</h1>"));
});
