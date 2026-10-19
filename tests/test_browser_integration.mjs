import assert from "node:assert/strict";
import test from "node:test";
import { api } from "../src/ma_runtime/web/static/api.mjs";
import { createIntegrationUI } from "../src/ma_runtime/web/static/integrations.js";
import { createSkillIntegration } from "../src/ma_runtime/web/static/skills.js";
import { content, compactTitle, schemaView, schemaDefaults, valueEditor, toolResult } from "../src/ma_runtime/web/static/presentation.mjs";
import { argumentOptions, describeLaunch, buildLaunch, createLaunchEditor } from "../src/ma_runtime/web/static/launch.mjs";
import { artifactDownload, artifactSource, groupArtifacts, decodeArtifactText, readArtifactPreview } from "../src/ma_runtime/web/static/artifacts.mjs";

test("successful task detail requests retain execution errors for display", async t => {
  for (const state of ["FAILED", "UNCERTAIN", "CANCELLED", "SUCCEEDED"]) {
    const task = { id: "task", state, error: state === "SUCCEEDED" ? null : {
      code: "ADAPTER_FAILURE", safe_message: "Execution stopped", details: { reason: "Unavailable" },
    } };
    const fetcher = t.mock.method(globalThis, "fetch", async url => {
      assert.equal(url, "/api/tasks/task");
      return Response.json(task);
    });
    try {
      assert.deepEqual(await api("/tasks/task"), task);
    } finally {
      fetcher.mock.restore();
    }
  }
});

test("HTTP failures still reject with the gateway error message and code", async t => {
  for (const status of [400, 401, 403, 404, 409, 422, 503]) {
    const fetcher = t.mock.method(globalThis, "fetch", async () => Response.json({
      error: { code: "REQUEST_FAILED", safe_message: "Request could not be completed" },
    }, { status }));
    try {
      await assert.rejects(api("/tasks/task"), {
        message: "Request could not be completed", code: "REQUEST_FAILED",
      });
    } finally {
      fetcher.mock.restore();
    }
  }
});

test("work artifacts group by Agent identity and keep uploads and unknown history separate", () => {
  const from = (id, agent_id, agent_name) => ({ id, metadata: { provenance: { kind: "agent", agent_id, agent_name } } });
  const items = [from("new", "writer", "Old writer name"), from("review", "reviewer", "Reviewer"),
    from("older", "writer", "Old writer name"), { id: "upload", metadata: { provenance: { kind: "upload" } } },
    { id: "external", metadata: { provenance: { kind: "external" } } }, { id: "legacy", metadata: { agent_id: "writer" } }];
  const groups = groupArtifacts(items, [{ id: "writer", name: "Writer" }, { id: "reviewer", name: "Writer" }]);
  assert.deepEqual(groups.map(group => group.source.key), ["agent:writer", "agent:reviewer", "upload", "external", "unknown"]);
  assert.deepEqual(groups[0].items.map(item => item.id), ["new", "older"]);
  assert.equal(groups[0].source.label, "Writer");
  assert.equal(groups[2].source.label, "用户上传");
  assert.equal(groups[4].source.label, "来源未记录");
  assert.equal(artifactSource(from("removed", "removed", "Archived writer")).label, "Archived writer");
});

test("artifact previews keep original Markdown, HTML, whitespace and empty text", async () => {
  const source = '# Note\r\n\r\n<script>example()</script>\r\n  **original**\tvalue';
  const bytes = new TextEncoder().encode(source);
  const preview = await readArtifactPreview({ id: "example/id", size: bytes.length, media_type: "text/markdown" }, {
    fetcher: async url => { assert.equal(url, artifactDownload("example/id")); return new Response(bytes); },
  });
  assert.deepEqual(preview, { kind: "text", text: source, encoding: "utf-8", truncated: false });
  assert.equal(decodeArtifactText(new Uint8Array()).text, "");
  assert.equal(decodeArtifactText(new Uint8Array([0xff, 0xfe, 0x9f, 0x53, 0x87, 0x65])).text, "原文");
  assert.equal(decodeArtifactText(new Uint8Array([0xfe, 0xff, 0x53, 0x9f, 0x65, 0x87])).text, "原文");
});

test("bounded artifact previews cancel the stream and retain complete Unicode characters", async () => {
  const bytes = new TextEncoder().encode("ab你好cd");
  let cancelled = false;
  const stream = new ReadableStream({ start(controller) { controller.enqueue(bytes); }, cancel() { cancelled = true; } });
  const preview = await readArtifactPreview({ id: "large", size: bytes.length }, { limit: 6, fetcher: async () => new Response(stream) });
  assert.equal(preview.text, "ab你");
  assert.equal(preview.truncated, true);
  assert.equal(cancelled, true);
});

test("binary and unsupported-encoding artifacts have an explicit download fallback", async () => {
  const preview = await readArtifactPreview({ id: "pdf", media_type: "application/pdf" }, { fetcher: async () => { throw new Error("Binary preview must not fetch content"); } });
  assert.equal(preview.kind, "unsupported");
  assert.equal(decodeArtifactText(new Uint8Array([0, 1, 2])).kind, "unsupported");
  assert.equal(decodeArtifactText(new Uint8Array([0xff, 0xff])).kind, "unsupported");
});

test("artifact download integrity errors propagate instead of showing a successful preview", async () => {
  await assert.rejects(readArtifactPreview({ id: "changed", size: 1 }, { fetcher: async () => new Response(JSON.stringify({ error: { safe_message: "Artifact checksum mismatch" } }), { status: 400, headers: { "content-type": "application/json" } }) }), /checksum mismatch/);
});

test("launch forms preserve existing command tokens, spaces, option order and empty values", () => {
  for (const command of [
    ["python", "-u", "-B", "agent with spaces.py", "--stage", "prepare", "--tag=one", "--tag", "two", "--name", ""],
    ["python3.12", "-B", "-m", "example.agent", "--limit", "-1", "--verbose", "--", "-literal"],
    ["node", "agent.mjs", "--mode=review"],
    ["python", "-X", "utf8", "agent.py", "--mode", "review"],
    ["pwsh", "-File", "agent.ps1", "--", "-literal"],
  ]) assert.deepEqual(buildLaunch(describeLaunch(command)), command);
  assert.equal(describeLaunch(["python", "-B", "agent.py"]).mode, "python-script");
  assert.equal(describeLaunch(["python", "-m", "example.agent"]).mode, "python-module");
  assert.equal(describeLaunch(["node", "agent.mjs"]).mode, "node-script");
  assert.equal(describeLaunch(["python", "-X", "utf8", "agent.py"]).mode, "program");
});

test("launch changes affect the selected field without shell joining or silent token loss", () => {
  const model = describeLaunch(["python", "-u", "-B", "agent.py", "--stage", "prepare", "--label=value with spaces"]);
  model.options[0].value = "review";
  model.flags = model.flags.filter(flag => flag !== "-B");
  assert.deepEqual(buildLaunch(model), ["python", "-u", "agent.py", "--stage", "review", "--label=value with spaces"]);
  assert.equal(argumentOptions(["--title="])[0].value, "");
  assert.throws(() => buildLaunch(describeLaunch([])), /入口文件/);
  model.options.push({ kind: "option", name: "", value: "value", enabled: true });
  assert.throws(() => buildLaunch(model), /选项名称/);
});

test("configured launches show a summary first and cancel restores the original settings", () => {
  const original = ["python", "-u", "-B", "agent.py", "--stage", "prepare"];
  const draft = { command: [...original] };
  const scope = { dataset: { launchScope: "agent" } };
  const ui = createLaunchEditor({ scope: "agent", getDraft: () => draft, read: d => d.command,
    write: (d, command) => { d.command = command; }, escape: String, icon: () => "", render: () => {}, purpose: "Task execution" });
  assert.ok(ui.panel().includes("Python 脚本"));
  assert.ok(!ui.panel().includes("<textarea"));
  assert.ok(!ui.panel().includes("data-launch-field"));
  assert.deepEqual(ui.commit(), original);
  const button = { closest: () => scope, dataset: {} };
  ui.action("launch-edit", button);
  ui.toggle({ isConnected: true, closest: () => scope, dataset: { launchDisclosure: "advanced" }, open: true });
  assert.ok(ui.panel().includes('data-launch-disclosure="advanced" open'));
  ui.input({ closest: () => scope, dataset: { launchField: "entry" }, value: "changed.py" });
  assert.deepEqual(draft.command, original);
  ui.action("launch-cancel", button);
  assert.deepEqual(ui.commit(), original);
  ui.action("launch-edit", button);
  ui.input({ closest: () => scope, dataset: { launchField: "entry" }, value: "changed.py" });
  ui.commit();
  assert.equal(draft.command[3], "changed.py");
});

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
  assert.ok(toolResult({ structuredContent: { text: "**Result**" }, content: [{ type: "text", text: "duplicate" }] }).includes("<strong>Result</strong>"));
  assert.ok(!toolResult({ structuredContent: { text: "Done" }, content: [{ type: "text", text: "duplicate" }] }).includes("duplicate"));
  assert.ok(toolResult({ content: [{ type: "text", text: "# Result" }] }).includes("<h1>Result</h1>"));
});

test("Codex setup displays the server-provided compact tools, environment and timeouts", async () => {
  const config = {
    command: "python", args: ["-B", "-m", "oea", "mcp", "--connect", "--compact", "--data-dir", "workspace"],
    startup_timeout_sec: 31, tool_timeout_sec: 181,
    enabled_tools: ["platform_guide", "platform_call"],
    env: { PYTHONUTF8: "1", PYTHONDONTWRITEBYTECODE: "1", CUSTOM_SETTING: 'a "quoted" value' },
  };
  const state = { mcpServers: [] };
  const requested = [];
  const ui = createIntegrationUI({ state, escape: String, icon: () => "", badge: () => "", markdown: String,
    api: async path => { requested.push(path); return { mcp_servers: { oea: config } }; },
    toast: () => {}, render: () => {}, renderModal: () => {}, idempotency: () => "request", loadBootstrap: async () => {}, time: String });
  await ui.action("mcp-sdk", { dataset: {} });
  assert.deepEqual(requested, ["/mcp/codex-config"]);
  assert.ok(state.modal.text.includes('enabled_tools = ["platform_guide","platform_call"]'));
  assert.ok(state.modal.text.includes(String.raw`"CUSTOM_SETTING" = "a \"quoted\" value"`));
  assert.ok(state.modal.text.includes("startup_timeout_sec = 31"));
  assert.ok(state.modal.text.includes("tool_timeout_sec = 181"));
  assert.ok(state.modal.sdk.includes('PlatformClient.from_data_dir("workspace")'));
});

test("Skill deregistration sends a JSON request accepted by the local gateway", async () => {
  const state = { skillDetail: { id: "review", bound_agents: [] } };
  let request;
  const ui = createSkillIntegration({ state, escape: String, icon: () => "", markdown: String,
    api: async (path, options) => { request = { path, ...options }; return { deleted: true }; },
    toast: () => {}, render: () => {}, loadBootstrap: async () => {} });
  await ui.action("registry-skill-delete", { dataset: { id: "review" } });
  assert.deepEqual(request, { path: "/skills/review", method: "DELETE", body: {} });
  assert.equal(state.skillDetail, null);
});
