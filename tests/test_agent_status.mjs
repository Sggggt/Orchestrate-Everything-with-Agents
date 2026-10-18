import assert from "node:assert/strict";
import test from "node:test";
import { createAgentStatusIndex } from "../src/ma_runtime/web/static/agent-status.mjs";

const worker = { id: "worker", kind: "mock", enabled: true };
const first = { id: "first", agent_id: worker.id }, second = { id: "second", agent_id: worker.id };
const task = (thread, state, created_at) => ({ thread_id: thread.id, state, created_at });
const project = (tasks, attention_thread_ids = [], agents = [worker], connection = "online") =>
  createAgentStatusIndex(agents, { threads: [first, second], tasks, attention_thread_ids }, connection);

test("a recovered thread stops showing its historical failure regardless of snapshot order", () => {
  const status = project([task(first, "FAILED", 1), task(first, "SUCCEEDED", 3), task(first, "FAILED", 2)]);
  assert.equal(status.forThread(first).tone, "ready");
  assert.equal(status.forAgent(worker.id).tone, "ready");
});

test("concurrent work cannot conceal an uncertain execution or another thread's failure", () => {
  let status = project([task(first, "RUNNING", 2), task(second, "FAILED", 1)]);
  assert.equal(status.forAgent(worker.id).tone, "error");
  assert.equal(status.forThread(first).tone, "running");
  status = project([task(first, "UNCERTAIN", 1), task(first, "SUCCEEDED", 2)], [second.id]);
  assert.equal(status.forAgent(worker.id).label, "待核对");
});

test("a latest failure remains visible after it leaves the bounded task history", () => {
  const status = createAgentStatusIndex([worker], {
    threads: [first, second], tasks: [task(second, "SUCCEEDED", 301)], failed_thread_ids: [first.id],
  });
  assert.equal(status.forThread(first).tone, "error");
  assert.equal(status.forAgent(worker.id).tone, "error");
});

test("unanswered reports remain visible after task success and clear when guidance arrives", () => {
  const tasks = [task(first, "SUCCEEDED", 1), task(second, "RUNNING", 2)];
  assert.equal(project(tasks, [first.id]).forAgent(worker.id).tone, "waiting");
  assert.equal(project(tasks).forAgent(worker.id).tone, "running");
  assert.equal(project([task(first, "WAITING", 3)]).forAgent(worker.id).tone, "waiting");
});

test("disconnecting masks stale readiness and execution until state is synchronized", () => {
  for (const tasks of [[], [task(first, "RUNNING", 1)]]) {
    assert.equal(project(tasks, [], [worker], "offline").forAgent(worker.id).tone, "muted");
    assert.match(project(tasks, [], [worker], "connecting").forThread(first).label, /待确认/);
  }
});

test("saved disablement or missing configuration does not pretend an active task stopped", () => {
  const disabled = { ...worker, enabled: false };
  assert.equal(project([], [], [disabled]).forAgent(worker.id).tone, "muted");
  assert.equal(project([task(first, "RUNNING", 1)], [], [disabled]).forAgent(worker.id).tone, "running");
  assert.equal(project([], [], [{ ...worker, kind: "llm" }]).forAgent(worker.id).label, "待配置模型");
  assert.equal(project([task(first, "QUEUED", 1)]).forAgent(worker.id).tone, "muted");
});
