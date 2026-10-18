import assert from "node:assert/strict";
import test from "node:test";
import { createIntegrationUI } from "../src/ma_runtime/web/static/integrations.js";
import { createSkillIntegration } from "../src/ma_runtime/web/static/skills.js";

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
