import assert from "node:assert/strict";
import test from "node:test";
import { createAgentStatusIndex } from "../src/ma_runtime/web/static/agent-status.mjs";
import { outcomeState, statusLabel, matchesTaskFilter } from "../src/ma_runtime/web/static/execution-status.mjs";

const worker = { id: "worker", kind: "mock", enabled: true };
const first = { id: "first", agent_id: worker.id }, second = { id: "second", agent_id: worker.id };
const task = (thread, state, created_at) => ({ thread_id: thread.id, state, created_at });
const project = (tasks, attention_thread_ids = [], agents = [worker], connection = "online", failed_thread_ids = []) =>
  createAgentStatusIndex(agents, { threads: [first, second], tasks, attention_thread_ids, failed_thread_ids }, connection);

test("a recovered thread stops showing its historical failure regardless of snapshot order", () => {
  const status = project([task(first, "FAILED", 1), task(first, "SUCCEEDED", 3), task(first, "FAILED", 2)]);
  assert.equal(status.forThread(first).tone, "ready");
  assert.equal(status.forAgent(worker.id).tone, "ready");
});

test("historical message failures do not mark a working Agent unavailable", () => {
  let status = project([task(first, "RUNNING", 2), task(second, "FAILED", 1)], [], [worker], "online", [second.id]);
  assert.equal(status.forAgent(worker.id).tone, "running");
  assert.equal(status.forThread(first).tone, "running");
  assert.equal(status.forThread({ thread_id: first.id, agent_id: worker.id }).tone, "running");
  status = project([task(first, "UNCERTAIN", 1), task(first, "SUCCEEDED", 2)], [second.id]);
  assert.equal(status.forThread(first).label, "暂不可继续");
  assert.equal(status.forAgent(worker.id).tone, "ready");
});

test("delivery feedback and model self-reports do not become Agent failures", () => {
  const incomplete = { ...task(first, "FAILED", 1), execution_state: "SUCCEEDED",
    result: { output: { status: "failed" } } };
  const status = project([incomplete]);
  assert.equal(status.forThread(first).tone, "ready");
  assert.equal(status.forAgent(worker.id).tone, "ready");
  assert.equal(outcomeState(incomplete), "FAILED");
  assert.equal(statusLabel(incomplete), "交付未通过");
  assert.equal(matchesTaskFilter(incomplete, "done"), false);
  assert.equal(matchesTaskFilter(incomplete, "attention"), true);
  const resumed = { ...incomplete, state: "RUNNING", execution_state: "RUNNING" };
  assert.equal(matchesTaskFilter(resumed, "attention"), false);
  assert.equal(matchesTaskFilter(resumed, "active"), true);
  const entry = { execution_state: "SUCCEEDED", metadata: { acceptance: { state: "FAILED" } } };
  assert.equal(statusLabel(entry), "交付未通过");
  assert.equal(statusLabel({ state: "SUCCEEDED", result: { output: { status: "FAILED" } } }), "已完成");
});

test("an old failure outside task history does not change current availability", () => {
  const status = createAgentStatusIndex([worker], {
    threads: [first, second], tasks: [task(second, "SUCCEEDED", 301)], failed_thread_ids: [first.id],
  });
  assert.equal(status.forThread(first).tone, "ready");
  assert.equal(status.forAgent(worker.id).tone, "ready");
});

test("reports do not replace availability while an active wait remains visible", () => {
  const tasks = [task(first, "SUCCEEDED", 1), task(second, "RUNNING", 2)];
  assert.equal(project(tasks, [first.id]).forAgent(worker.id).tone, "running");
  assert.equal(project(tasks).forAgent(worker.id).tone, "running");
  assert.equal(project([task(first, "WAITING", 3)]).forAgent(worker.id).tone, "waiting");
});

test("disconnecting masks stale readiness and execution until state is synchronized", () => {
  for (const tasks of [[], [task(first, "RUNNING", 1)]]) {
    assert.equal(project(tasks, [], [worker], "offline").forAgent(worker.id).tone, "muted");
    assert.match(project(tasks, [], [worker], "connecting").forThread(first).label, /待确认/);
  }
});

test("disablement and missing configuration prevent advertising availability", () => {
  const disabled = { ...worker, enabled: false };
  assert.equal(project([], [], [disabled]).forAgent(worker.id).tone, "muted");
  assert.equal(project([task(first, "RUNNING", 1)], [], [disabled]).forAgent(worker.id).tone, "muted");
  assert.equal(project([], [], [{ ...worker, kind: "llm" }]).forAgent(worker.id).label, "待配置模型");
  assert.equal(project([task(first, "QUEUED", 1)]).forAgent(worker.id).tone, "muted");
});

test("a blocked instance cannot continue but does not block other Agent conversations", () => {
  const blocked = { ...first, blocked_reason: "OUTCOME_UNCERTAIN" };
  const status = createAgentStatusIndex([worker], { threads: [blocked, second], tasks: [] });
  assert.equal(status.forThread(blocked).label, "暂不可继续");
  assert.equal(status.forThread(second).label, "可对话");
  assert.equal(status.forAgent(worker.id).label, "可对话");
  assert.equal(status.forThread({ thread_id: "paged", agent_id: worker.id, execution_state: "WAITING" }).label, "等待中");
});
