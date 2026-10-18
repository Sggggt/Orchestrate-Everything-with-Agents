import assert from "node:assert/strict";
import test from "node:test";
import { agentFeedback, messageItems, matchesMessageFilter } from "../src/ma_runtime/web/static/message-feed.mjs";
import { markdown } from "../src/ma_runtime/web/static/markdown.mjs";

const message = (id, created_at, run = "run") => ({ id, created_at, state: "PENDING", source_thread_id: "child",
  envelope: { source_run_id: run, payload: { summary_markdown: "### Progress\n\n**Ready**\n\n- Verified" } } });
const report = (id, created_at, extra = {}) => ({ id, created_at, thread_id: "child", task_id: "task",
  summary_markdown: "**Needs guidance**", replied: false, wait_for_guidance: true, ...extra });

test("feedback selection returns the latest three Agent messages/reports, excluding external coordinator messages", () => {
  const snapshot = { messages: [message("old", 1), message("external", 9, null), message("recent", 4)],
    agent_reports: [report("older-report", 2), report("new-report", 5)] };
  assert.deepEqual(agentFeedback(messageItems(snapshot)).map(item => item.id), ["new-report", "recent", "older-report"]);
  assert.equal(messageItems(snapshot).length, 5);
});

test("report filtering and acknowledgement labels do not pretend guidance resolved the problem", () => {
  const snapshot = { messages: [message("message", 1)], agent_reports: [report("waiting", 2), report("answered", 3, { replied: true })] };
  const reports = messageItems(snapshot).filter(item => matchesMessageFilter(item, "reports"));
  assert.deepEqual(reports.map(item => item.state), ["REPLIED", "GUIDANCE"]);
  assert.equal(messageItems(snapshot).filter(item => matchesMessageFilter(item, "PENDING")).length, 1);
});

test("Agent summaries retain Markdown and use the existing safe renderer", () => {
  const item = agentFeedback(messageItems({ messages: [message("message", 1)] }))[0];
  const html = markdown(item.summary_markdown + '\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert)');
  assert.ok(html.includes("<h3>Progress</h3>"));
  assert.ok(html.includes("<strong>Ready</strong>"));
  assert.ok(html.includes("<ul>"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes('href="javascript:'));
  assert.deepEqual(agentFeedback(messageItems({ messages: [] })), []);
});
