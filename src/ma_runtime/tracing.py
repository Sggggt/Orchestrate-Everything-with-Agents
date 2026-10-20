"""Small durable model spans; raw prompts and credentials never enter traces."""

import time

from .models import new_id, now_ms
from .store import emit


class RunStage:
    def __init__(self, context, name, **attributes):
        self.context, self.name = context, name
        self.id, self.started_at = new_id("stage"), now_ms()
        self.started = time.perf_counter()
        self.attributes = attributes

    def elapsed(self):
        return round((time.perf_counter() - self.started) * 1000, 3)

    async def record(self, state, error=None):
        runtime, request = self.context._runtime, getattr(self.context, "request", None)
        # Saved-model connection probes have no task/run and keep only their latency.
        if request is None:
            return
        payload = {"id": self.id, "stage": self.name, "state": state, "started_at": self.started_at,
                   "attributes": self.attributes}
        if state != "RUNNING":
            payload.update(ended_at=now_ms(), duration_ms=self.elapsed())
        if error is not None:
            payload["error"] = error.as_dict()
        safe = runtime.clean_json(payload, limit=65536)
        def save(db):
            runtime.check_run(db, request.run_id)
            if request.continuation and state != "RUNNING" and self.attributes.get("max_output_tokens"):
                runtime.continuations.settle_model(db, request.task_id, self.attributes["max_output_tokens"], self.attributes.get("usage"))
            emit(db, "run.stage", session_id=request.session_id, thread_id=request.thread_id,
                 task_id=request.task_id, run_id=request.run_id, payload=safe)
        await runtime.store.call(save, transaction=True)
        runtime.notify()
