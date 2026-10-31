"""Persist model receipts before acknowledging their graph node."""

import asyncio
import hashlib
import json

from oea.contracts.measurement import measurement, content_view
from oea.contracts.domain import Completion
from oea.contracts.errors import RuntimeFault, require
from oea.models.api import ModelFailure, ModelCancelled
from oea.models.recovery import input_fingerprint, saved_fault, window_rejected
from oea.models.stream import ResponseStreams
from oea.storage.persistence import encode, event, new_id, now_ms, one


def saved_output_measurement(value):
    message = value.get("message", {})
    size = len((message.get("content") or "").encode("utf-8"))
    for call in message.get("tool_calls", []) + value.get("discarded_tool_calls", []):
        size += sum(len(part.encode("utf-8")) for part in (call["id"], call["function"]["name"], call["function"]["arguments"]))
    return measurement("saved_output_bytes", size, "byte", "measured")


def input_comparison(projection, usage):
    reported = usage.get("prompt_tokens", usage.get("input_tokens"))
    reported = reported if type(reported) is int and reported >= 0 else None
    return {"projection_revision": projection["projection_revision"], "predicted_input_tokens": projection["input_tokens"],
            "provider_input_tokens": reported, "difference_tokens": reported - projection["input_tokens"] if reported is not None else None,
            "method": projection.get("method"), "model_fingerprint": projection["model_fingerprint"], "tools_fingerprint": projection["tools_fingerprint"]}


class ModelExecutor:
    def __init__(self, host):
        self.host = host
        self.streams = ResponseStreams()

    async def complete(self, context, key, build_input, *, purpose="model", output_tokens=None, conversation=True, recovery=None):
        host, run_id = self.host, context.run["id"]
        requested_output = context.definition.model.max_output_tokens if output_tokens is None else output_tokens
        def lookup(db):
            previous = one(db, "SELECT * FROM actions WHERE run_id=? AND operation_key=?", (run_id, key))
            # A local input ceiling without a configured model window does
            # not depend on output reserve; its transaction remains the sole
            # budget aggregate on the normal path.
            allowance = requested_output if (previous and previous["state"] == "completed" or context.definition.model.context_window_tokens is None) else host.budgets.output_allowance(db, context.run, requested_output)
            return previous, allowance
        previous, planned_output = await host.store.call(lookup)
        if previous and previous["state"] == "completed":
            return previous["id"], json.loads(previous["result_json"])
        await host.authorize_model(context)
        reviewed = False
        if previous:
            require(previous["state"] in {"intent", "failed"}, "MODEL_IN_FLIGHT", "The original model invocation has not settled", run_id=run_id, action_id=previous["id"])
            failure = await host.store.call(lambda db: one(db, "SELECT MAX(position) AS position FROM events WHERE run_id=? AND kind='model.failed' AND json_extract(data_json,'$.action_id')=?", (run_id, previous["id"])))
            reviewed = previous["state"] == "intent" or context.resumption and context.resumption["event_position"] > (failure["position"] or 0)
            if not reviewed and not recovery and window_rejected(json.loads(previous["error_json"] or "{}"), json.loads(previous["result_json"] or "{}")):
                raise saved_fault(json.loads(previous["error_json"]))
            require(reviewed or recovery, "MODEL_REVIEW_REQUIRED",
                    "Inspect the original model request before explicitly resuming this work", run_id=run_id, action_id=previous["id"])
        require(planned_output > 0, "BUDGET_EXHAUSTED", "Work output-token budget is exhausted", origin="local", stage="prepare")
        messages, tools, projection = await build_input(output_tokens=planned_output)
        contracts = context.request_contracts if projection.get("tool_contracts") else {}
        require(not host.vault.contains_secret(tools), "TOOL_SCHEMA_CONTAINS_SECRET", "Tool metadata contains protected credentials; correct its declaration before calling a model")
        fingerprint = input_fingerprint(messages, tools, context.definition.model)
        projection.update(projection_revision=fingerprint,
            model_fingerprint=hashlib.sha256(encode(context.definition.model).encode("utf-8")).hexdigest(),
            tools_fingerprint=hashlib.sha256(encode(projection.get("tool_contracts", {})).encode("utf-8")).hexdigest())
        action_id = previous["id"] if previous else new_id("model")
        usage_id = new_id("usage")
        facts = {"attempt_id": usage_id, "prepared_at": None, "send_started_at": None, "response_received_at": None,
                 "terminal_at": None, "send_started": None, "response_received": None, "provider_accepted": None}
        metadata = {"attempt": facts, "model": context.definition.model.model_dump(mode="json"), "purpose": purpose, "input_hash": fingerprint,
                    "tool_names": [tool["function"]["name"] for tool in tools], "projection": projection}
        if recovery and not reviewed:
            metadata["continuation"] = {"kind": "context_recovery", "attempt_id": recovery["attempt_id"]}
        elif previous and reviewed:
            metadata["continuation"] = {"kind": "explicit_resume", "resumption_id": context.resumption["id"]} if context.resumption else {"kind": "unsent_intent"}

        def dispatch(db):
            run = one(db, "SELECT * FROM runs WHERE id=?", (run_id,))
            require(run and run["state"] == "running" and run["cancel_requested_at"] is None,
                    "STALE_RUN", "Run is no longer allowed to call a model")
            if recovery and not reviewed:
                require(not one(db, "SELECT 1 FROM actions WHERE run_id=? AND id<>? AND side_effects!='none' AND state IN ('intent','dispatched','needs_review') LIMIT 1", (run_id, action_id)),
                        "MODEL_REVIEW_REQUIRED", "Unsettled side effects must be inspected before context recovery", run_id=run_id, action_id=action_id)
                evidence = one(db, "SELECT data_json FROM events WHERE run_id=? AND kind='context.recovered' AND json_extract(data_json,'$.action_id')=? ORDER BY position DESC LIMIT 1", (run_id, action_id))
                original = json.loads(previous["result_json"] or "{}") if previous else {}
                require(evidence and json.loads(evidence["data_json"]) == recovery and previous["state"] == "failed"
                        and window_rejected(json.loads(previous["error_json"] or "{}"), original)
                        and original["attempt_facts"].get("attempt_id") == recovery["attempt_id"]
                        and previous["input_hash"] == recovery["original_input_hash"] and fingerprint == recovery["input_hash"]
                        and projection["input_tokens"] < recovery["input_tokens_before"],
                        "MODEL_REVIEW_REQUIRED", "Context recovery no longer matches the rejected attempt and reduced input", action_id=action_id)
            prior_usage = one(db, "SELECT id,created_at FROM usage WHERE action_id=? AND kind='model' ORDER BY created_at DESC,rowid DESC LIMIT 1", (action_id,)) if previous and previous["state"] == "failed" else None
            require(not previous or previous["state"] != "failed" or prior_usage is not None, "MODEL_RECEIPT_UNAVAILABLE", "Original model usage evidence is unavailable")
            reserve = host.budgets.model(db, run, usage_id, purpose, projection["input_tokens"], planned_output)
            budget = context.definition.context.input_tokens
            window = context.definition.model.context_window_tokens
            if window is not None:
                budget = min(budget, window - reserve - context.definition.context.safety_tokens)
            projection.update(budget_tokens=budget, output_reserved_tokens=reserve,
                              measurement=measurement("model_input_tokens", projection["input_tokens"], "token", "heuristic_estimate", limit=budget, limit_source="context.input_tokens" if window is None else "min(context.input_tokens,model.context_window_tokens-output_reserve-safety)"))
            require(projection["input_tokens"] <= budget, "MODEL_CONTEXT_BUDGET", "Prepared input exceeds its effective token estimate budget", measurement=projection["measurement"], origin="local", stage="prepare", details={"components": projection.get("components")})
            db.execute("UPDATE usage SET action_id=? WHERE id=?", (action_id, usage_id))
            for name, contract_id in projection.get("tool_contracts", {}).items():
                db.execute("INSERT OR IGNORE INTO tool_contracts VALUES(?,?)", (contract_id, contracts[name][1]))
            if previous:
                if prior_usage:
                    db.execute("INSERT INTO model_failures VALUES(?,?,?,?,?,?,?)", (prior_usage["id"], action_id, previous["input_hash"], previous["input_json"], previous["result_json"], previous["error_json"], prior_usage["created_at"]))
                db.execute("UPDATE actions SET input_hash=?,input_json=?,state='dispatched',result_json=NULL,error_json=NULL,updated_at=? WHERE id=?",
                           (fingerprint, encode(metadata), now_ms(), action_id))
            else:
                db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,error_json,created_at,updated_at) VALUES(?,?,?,'model.complete',?,?,'none','dispatched',NULL,NULL,?,?)",
                           (action_id, run_id, key, fingerprint, encode(metadata), now_ms(), now_ms()))
            position = event(db, "model.started", project_id=context.instance["project_id"], session_id=context.instance["session_id"],
                  instance_id=context.instance["id"], run_id=run_id,
                  data={"action_id": action_id, "usage_id": usage_id, "attempt_id": usage_id, "purpose": purpose, "model_role": context.instance["role"], "projection": projection})
            return reserve, position

        reserve, position = await host.store.call(dispatch, transaction=True)
        stream = self.streams.start(run_id, action_id, usage_id, position)
        host.notify()
        async def emit(frame):
            if purpose != "model":
                return
            if frame["kind"] == "model.delta":
                value = self.streams.append(stream, host.redact(frame["text"]))
                if value:
                    await host.emit_progress(run_id, value)
            else:
                await host.emit_progress(run_id, {**frame, "action_id": action_id, "attempt_id": usage_id, "attempt_position": position})
        async def record_fact(stage, observed, request_measurement):
            facts.update(observed)
            metadata["request_measurement"] = request_measurement
            def save(db):
                db.execute("UPDATE actions SET input_json=?,updated_at=? WHERE id=? AND json_extract(input_json,'$.attempt.attempt_id')=?", (encode(metadata), now_ms(), action_id, usage_id))
                if db.changes():
                    event(db, "model." + stage, run_id=run_id, instance_id=context.instance["id"],
                          data={"action_id": action_id, "attempt_id": usage_id, "facts": dict(facts), "measurement": request_measurement})
            await host.store.call(save, transaction=True)
            host.notify()
        api_started = False
        try:
            await host.authorize_model(context)
            wire_messages = await context.model_messages(messages)
            api_started = True
            completion = await host.model_api.complete(context.definition.model, wire_messages, tools=tools, output_tokens=reserve,
                emit=emit, on_fact=record_fact)
            completion = Completion.model_validate(completion)
        except BaseException as error:
            fault = error if isinstance(error, RuntimeFault) else RuntimeFault(
                "MODEL_CANCELLED" if isinstance(error, asyncio.CancelledError) else "MODEL_FAILED",
                "Model call did not complete", details={"cause": type(error).__name__})
            facts.update(fault.details.get("attempt_facts", {}))
            if not api_started or fault.details.get("request_sent") is False:
                facts.update(send_started=False, response_received=False)
            facts.update(terminal_at=now_ms(), outcome="failed", generation_completed=False)
            fault.identity.update(run_id=run_id, instance_id=context.instance["id"], action_id=action_id,
                                  usage_id=usage_id, attempt_id=usage_id, purpose=purpose, model_role=context.instance["role"], evidence_ref={"action_id": action_id, "attempt_id": usage_id}, continuation="inspect_then_resume")
            fault.identity.setdefault("stage", "model")
            fault.details["attempt_facts"] = dict(facts)
            partial = error.partial if isinstance(error, (ModelFailure, ModelCancelled)) else None
            fault.details["integrity"] = partial.content_view["integrity"] if partial and partial.content_view else "unknown"
            partial_value = {"message": {"role": "assistant", "content": partial.text}, "usage": partial.usage,
                             "finish_reason": "interrupted", "discarded_tool_calls": list(partial.tool_calls),
                             "measurements": partial.measurements, "content_view": partial.content_view or content_view("full", integrity="partial", source_complete=False),
                             "attempt_facts": dict(facts)} if partial else {"attempt_facts": dict(facts), "usage": {}, "content_view": content_view("full", integrity="unknown", source_complete=None)}
            partial_value = host.redact(partial_value)
            partial_value["content_view"]["source_ref"] = {"action_id": action_id, "attempt_id": usage_id}
            partial_value["measurements"] = [*partial_value.get("measurements", ()), saved_output_measurement(partial_value)]
            partial_value["input_comparison"] = input_comparison(projection, partial_value["usage"])
            def fail(db):
                if window_rejected(fault.as_dict(), partial_value):
                    host.budgets.reject_before_send(db, usage_id)
                elif facts["send_started"] is not False:
                    db.execute("UPDATE usage SET state='needs_review' WHERE id=?", (usage_id,))
                else:
                    host.budgets.reject_before_send(db, usage_id)
                if partial and partial.usage:
                    host.budgets.settle(db, usage_id, partial.usage)
                db.execute("UPDATE actions SET state='failed',input_json=?,result_json=?,error_json=?,updated_at=? WHERE id=? AND json_extract(input_json,'$.attempt.attempt_id')=?",
                           (encode(metadata), encode(partial_value), encode(host.redact(fault.as_dict())), now_ms(), action_id, usage_id))
                if db.changes():
                    event(db, "model.failed", run_id=run_id, instance_id=context.instance["id"],
                          data={"action_id": action_id, "usage_id": usage_id, "attempt_id": usage_id, "error": host.redact(fault.as_dict()), "partial_available": bool(partial)})
            await host.store.call(fail, transaction=True)
            self.streams.finish(stream)
            host.notify()
            if isinstance(error, asyncio.CancelledError) or error is fault:
                raise
            raise fault from error
        message = {"role": "assistant", "content": completion.text if not completion.tool_calls else completion.text or None}
        if completion.tool_calls and completion.finish_reason in {"tool_calls", "stop"}:
            message["tool_calls"] = list(completion.tool_calls)
        facts.update(completion.attempt_facts)
        facts.update(terminal_at=now_ms(), outcome="completed", generation_completed=completion.finish_reason in {"stop", "tool_calls"})
        integrity = "incomplete" if completion.finish_reason in {"length", "max_tokens"} else "complete"
        result = host.redact({"message": message, "finish_reason": completion.finish_reason, "usage": completion.usage,
                             "attempt_facts": dict(facts), "measurements": completion.measurements,
                             "content_view": completion.content_view or content_view("full", integrity=integrity, source_complete=integrity == "complete")})
        if completion.tool_calls and not message.get("tool_calls"):
            result["discarded_tool_calls"] = host.redact(list(completion.tool_calls))
        result["content_view"]["source_ref"] = {"action_id": action_id, "attempt_id": usage_id}
        result["measurements"] = [*result["measurements"], saved_output_measurement(result)]
        result["input_comparison"] = input_comparison(projection, completion.usage)
        def complete(db):
            host.budgets.settle(db, usage_id, completion.usage)
            db.execute("UPDATE actions SET state='completed',input_json=?,result_json=?,error_json=NULL,updated_at=? WHERE id=? AND json_extract(input_json,'$.attempt.attempt_id')=?",
                       (encode(metadata), encode(result), now_ms(), action_id, usage_id))
            if not db.changes():
                return
            if conversation:
                db.execute("INSERT OR IGNORE INTO conversation(id,instance_id,run_id,role,body_json,action_id,created_at) VALUES(?,?,?,'assistant',NULL,?,?)",
                           (key, context.instance["id"], run_id, action_id, now_ms()))
            event(db, "model.completed", project_id=context.instance["project_id"], session_id=context.instance["session_id"],
                  instance_id=context.instance["id"], run_id=run_id,
                  data={"action_id": action_id, "usage_id": usage_id, "purpose": purpose, "finish_reason": completion.finish_reason, "attempt_id": usage_id, "facts": dict(facts)})
        await host.store.call(complete, transaction=True)
        self.streams.finish(stream)
        host.notify()
        return action_id, result
