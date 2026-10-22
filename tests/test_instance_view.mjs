import assert from "node:assert/strict";
import test from "node:test";
import { instanceGroups, instanceInUse, conversationItems, mergeHistoryPage, orderedRuns } from "../src/ma_runtime/web/static/instance-view.mjs";

test("live updates preserve loaded older messages and their paging cursor", () => {
  const recent = { items: [{ id: "c", seq: 3 }, { id: "d", seq: 4 }], cursor: 3, has_more: true };
  const latest = { items: [{ id: "d", seq: 4, state: "SUCCEEDED" }, { id: "e", seq: 5 }], cursor: 4, has_more: true };
  assert.equal(mergeHistoryPage(recent, latest), latest);
  const history = mergeHistoryPage(recent, { items: [{ id: "a", seq: 1 }, { id: "b", seq: 2 }], cursor: null, has_more: false }, true);
  const refreshed = mergeHistoryPage(history, latest);
  assert.deepEqual(refreshed.items.map(item => item.id), ["a", "b", "c", "d", "e"]);
  assert.equal(refreshed.items[3].state, "SUCCEEDED");
  assert.equal(refreshed.cursor, null);
  assert.equal(refreshed.has_more, false);
  const activity = mergeHistoryPage({ items: [{ position: 2, read: false }], cursor: "older", has_more: true },
    { items: [{ position: 1 }] }, true, "position");
  const updated = mergeHistoryPage(activity, { items: [{ position: 2, read: true }, { position: 3 }] }, false, "position");
  assert.deepEqual(updated.items.map(item => item.position), [1, 2, 3]);
  assert.equal(updated.items[1].read, true);
});

test("current ownership keeps instances in use while settled messages leave them idle", () => {
  for (const execution_state of ["RUNNING", "WAITING", "QUEUED", "UNCERTAIN"])
    assert.equal(instanceInUse({ execution_state }), true);
  for (const execution_state of ["READY", "SUCCEEDED", "FAILED", "CANCELLED"])
    assert.equal(instanceInUse({ execution_state }), false);
  assert.equal(instanceInUse({ execution_state: "SUCCEEDED", active_run_id: "active" }), true);
  assert.equal(instanceInUse({ owner_task_id: "suspended" }), true);
});

test("Agent activity orders groups while preserving mixed states and per-thread unread", () => {
  const agents = [{ id: "a" }, { id: "b" }];
  const snapshot = { threads: [
    { id: "one", agent_id: "a", created_at: 1 }, { id: "two", agent_id: "a", created_at: 2 },
    { id: "three", agent_id: "b", created_at: 3 },
  ], thread_activity: [
    { thread_id: "one", activity_position: 10, unread_count: 2, execution_state: "RUNNING" },
    { thread_id: "two", activity_position: 11, unread_count: 1, execution_state: "FAILED" },
    { thread_id: "three", activity_position: 12, unread_count: 0, execution_state: "WAITING" },
  ] };
  const groups = instanceGroups(agents, snapshot);
  assert.deepEqual(groups.map(group => group.agent.id), ["b", "a"]);
  assert.equal(groups[1].unread_count, 3);
  assert.equal(groups[1].counts.RUNNING, 1);
  assert.equal(groups[1].counts.FAILED, 1);
  assert.deepEqual(groups[1].instances.map(item => item.thread_id), ["two", "one"]);
  snapshot.progress = [{ thread_id: "one", created_at: 99999 }];
  assert.deepEqual(instanceGroups(agents, snapshot).map(group => group.agent.id), ["b", "a"]);
});

test("only corresponding conversation messages acquire read positions", () => {
  const result = conversationItems({ items: [{ id: "old", created_at: 1 }, { id: "answer", created_at: 3 }] },
    { items: [{ source_id: "answer", kind: "conversation", position: 8, created_at: 3 },
      { source_id: "report", kind: "report", position: 7, created_at: 2 }] });
  assert.deepEqual(result.map(item => item.id), ["old", "report", "answer"]);
  assert.equal(result[0].activity, undefined);
  assert.equal(result[1].notice, true);
  assert.equal(result[2].activity.position, 8);
});

test("run ordering puts executing instances first with deterministic ties", () => {
  const input = [{ id: "done", state: "SUCCEEDED", created_at: 10 },
    { id: "wait", state: "WAITING", created_at: 20 }, { id: "a", state: "RUNNING", created_at: 1 },
    { id: "b", state: "RUNNING", created_at: 1 }];
  assert.deepEqual(orderedRuns(input).map(run => run.id), ["b", "a", "wait", "done"]);
  assert.equal(input[0].id, "done");
});
