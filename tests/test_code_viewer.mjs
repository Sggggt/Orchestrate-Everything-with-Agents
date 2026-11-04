import assert from "node:assert/strict";
import test from "node:test";
import { highlightLine } from "../src/oea/web/static/code-viewer.mjs";

test("source highlighting keeps untrusted code inert",()=>{
  const value='value = "</code><img src=x onerror=alert(1)>" # <script>bad()</script>';
  const html=highlightLine(value);
  assert.ok(html.includes("&lt;img"));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script>"));
});

test("source highlighting preserves literal escape sequences and Unicode",()=>{
  const html=highlightLine(String.raw`path = "C:\new\file.py"`);
  assert.ok(html.includes(String.raw`C:\new\file.py`));
  assert.ok(!html.includes("\n"));
  assert.ok(highlightLine("message = '中文说明'").includes("中文说明"));
});
