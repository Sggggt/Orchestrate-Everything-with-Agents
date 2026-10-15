"""Durable cursor catch-up plus bounded, expendable live progress."""

import asyncio
import json
from collections import OrderedDict

from .models import PageEnvelope, RuntimeEvent, View, new_id, now_ms
from .store import one, rows


def event_view(row):
    result = View(row)
    result["event_id"] = result.pop("id")
    result["payload"] = json.loads(result.pop("payload_json"))
    result["schema_version"] = "1.0"
    result["durable"] = True
    return RuntimeEvent.model_validate(result)


class EventHub:
    def __init__(self, runtime):
        self.runtime = runtime
        self.subscribers = {}
        self.summaries = OrderedDict()

    def notify(self):
        for queue in tuple(self.subscribers):
            if not queue.full():
                queue.put_nowait(None)

    def progress(self, session_id, thread_id, task_id, run_id, event_type, payload):
        if event_type == "run.progress" and isinstance(payload.get("delta"), str):
            current = self.summaries.get(run_id, {"session_id": session_id, "thread_id": thread_id,
                "task_id": task_id, "run_id": run_id, "summary_markdown": ""})
            current["summary_markdown"] = (current["summary_markdown"] + self.runtime.vault.redact(payload["delta"]))[:1600]
            current["updated_at"] = now_ms()
            self.summaries[run_id] = current
            self.summaries.move_to_end(run_id)
            while len(self.summaries) > 256:
                self.summaries.popitem(last=False)
        event = {
            "schema_version": "1.0",
            "seq": None,
            "event_id": new_id("live"),
            "session_id": session_id,
            "thread_id": thread_id,
            "task_id": task_id,
            "run_id": run_id,
            "type": event_type,
            "created_at": now_ms(),
            "durable": False,
            "payload": self.runtime.vault.redact(payload),
        }
        for queue, state in tuple(self.subscribers.items()):
            if state["session_id"] != session_id:
                continue
            if queue.full():
                state["overflow"] = True
            else:
                queue.put_nowait(event)

    async def live(self, session_id):
        await self.runtime.sessions.get(session_id)
        running = await self.runtime.store.call(lambda db: rows(db,
            "SELECT id FROM tasks WHERE session_id=? AND state='RUNNING'", (session_id,)))
        ids = {row.id for row in running}
        return [dict(v) for v in self.summaries.values()
                if v["session_id"] == session_id and v["task_id"] in ids]

    async def list(self, session_id: str, after_seq: int = 0, limit: int = 100):
        await self.runtime.sessions.get(session_id)
        limit = max(1, min(int(limit), 500))
        found = await self.runtime.store.call(
            lambda db: rows(
                db,
                "SELECT * FROM events WHERE (session_id=? OR session_id IS NULL) AND seq>? ORDER BY seq LIMIT ?",
                (session_id, max(0, int(after_seq)), limit + 1),
            )
        )
        more = len(found) > limit
        items = [event_view(item) for item in found[:limit]]
        return PageEnvelope(
            items=items, cursor=items[-1]["seq"] if items else after_seq, has_more=more
        )

    async def subscribe(self, session_id: str, after_seq: int = 0):
        await self.runtime.sessions.get(session_id)
        queue = asyncio.Queue(maxsize=self.runtime.config.subscriber_buffer_size)
        state = {"session_id": session_id, "overflow": False}
        self.subscribers[queue] = state
        cursor = max(0, int(after_seq))
        try:
            maximum = await self.runtime.store.call(
                lambda db: one(db, "SELECT COALESCE(MAX(seq),0) AS seq FROM events").seq
            )
            if cursor > maximum:
                yield {
                    "type": "stream.resync",
                    "durable": False,
                    "seq": None,
                    "session_id": session_id,
                    "payload": {"code": "RESYNC_REQUIRED"},
                }
                return
            while not self.runtime.closed:
                batch = await self.list(session_id, cursor, limit=128)
                for event in batch["items"]:
                    cursor = event["seq"]
                    yield event
                if batch.has_more:
                    continue
                if state["overflow"]:
                    state["overflow"] = False
                    yield {
                        "type": "stream.resync",
                        "durable": False,
                        "seq": None,
                        "session_id": session_id,
                        "payload": {"code": "RESYNC_REQUIRED"},
                    }
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1)
                except TimeoutError:
                    yield {
                        "type": "stream.heartbeat",
                        "durable": False,
                        "seq": None,
                        "session_id": session_id,
                        "payload": {},
                    }
                    continue
                if item is not None:
                    yield item
        finally:
            self.subscribers.pop(queue, None)
