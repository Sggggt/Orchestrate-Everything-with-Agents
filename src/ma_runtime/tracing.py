"""Small durable model spans; raw prompts and credentials never enter traces."""

import time

from .errors import RuntimeFault
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
        def save(db):
            runtime.check_run(db, request.run_id)
            if "input_tokens_estimate" in self.attributes:
                if state == "RUNNING":
                    maximum = self.attributes.get("max_output_tokens")
                    # Both reservations commit with the trace, or neither commits.
                    if request.continuation:
                        maximum = runtime.continuations.reserve_model(db, request, maximum)
                    self.attributes["max_output_tokens"] = runtime.budgets.reserve_model(
                        db, request, self.id, self.name, self.attributes["input_tokens_estimate"], maximum)
                    if request.continuation and maximum != self.attributes["max_output_tokens"]:
                        runtime.continuations.settle_model(db, request.task_id, maximum,
                            {"completion_tokens": self.attributes["max_output_tokens"]})
                else:
                    runtime.budgets.settle_model(db, self.id, self.attributes.get("usage"))
                    if request.continuation:
                        runtime.continuations.settle_model(db, request.task_id, self.attributes["max_output_tokens"], self.attributes.get("usage"))
            safe = runtime.clean_json(payload, limit=65536)
            if state != "RUNNING" and self.attributes.get("model_fingerprint"):
                usage = self.attributes.get("usage") or {}
                incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
                outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
                db.execute("INSERT INTO model_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (self.id, request.agent_id, request.task_id, self.attributes["model_fingerprint"],
                            state, payload["duration_ms"], incoming if type(incoming) is int and incoming >= 0 else None,
                            outgoing if type(outgoing) is int and outgoing >= 0 else None,
                            int(self.attributes.get("tools_requested", False)), len(self.attributes.get("tool_calls", [])), payload["ended_at"]))
            emit(db, "run.stage", session_id=request.session_id, thread_id=request.thread_id,
                 task_id=request.task_id, run_id=request.run_id, payload=safe)
        try:
            await runtime.store.call(save, transaction=True)
        except RuntimeFault as exc:
            runtime.budgets.reject(self.context, exc)
            raise
        runtime.notify()
