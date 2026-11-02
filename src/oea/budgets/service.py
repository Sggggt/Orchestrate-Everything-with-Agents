"""Intent-owned usage accounting; never an execution admission mechanism."""

import hashlib
import json
from decimal import Decimal

from oea.contracts.configuration import Budget, Model
from oea.catalog.service import need
from oea.contracts.errors import require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows
from oea.budgets.measurement import normalize, charge, micros, upper_input_rate


class Budgets:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _create(db, policy, root_run_id=None, *, root_workflow_run_id=None, project_id, session_id=None, idempotency_key=None, fingerprint=None):
        policy = policy if isinstance(policy, Budget) else Budget.model_validate(policy)
        identity = new_id("budget")
        db.execute("INSERT INTO budgets(id,project_id,session_id,root_run_id,root_workflow_run_id,policy_json,created_at,idempotency_key,input_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                   (identity, project_id, session_id, root_run_id, root_workflow_run_id, encode(policy), now_ms(), idempotency_key, fingerprint))
        return identity

    async def create(self, project_id: str, policy: Budget, idempotency_key: str, *, session_id: str | None = None):
        """Explicit shared cost budget, scoped before any work is accepted."""
        policy = Budget.model_validate(policy)
        require(0 < len(idempotency_key) <= 256, "INVALID_REQUEST", "A shared budget needs a stable key")
        fingerprint = hashlib.sha256(encode({"policy": policy, "session_id": session_id}).encode("utf-8")).hexdigest()
        def accept(db):
            need(db, "projects", project_id)
            require(session_id is None or need(db, "sessions", session_id)["project_id"] == project_id, "PROJECT_MISMATCH", "Budget session belongs to another project")
            previous = one(db, "SELECT id,input_hash FROM budgets WHERE project_id=? AND idempotency_key=?", (project_id, idempotency_key))
            if previous:
                require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Budget key has different policy or scope")
                return previous["id"]
            return self._create(db, policy, project_id=project_id, session_id=session_id, idempotency_key=idempotency_key, fingerprint=fingerprint)
        identity = await self.store.call(accept, transaction=True)
        return await self.get(identity)

    @staticmethod
    def require_scope(db, budget_id, *, project_id, session_id):
        budget = need(db, "budgets", budget_id)
        require(budget["project_id"] == project_id and budget["session_id"] in {None, session_id}, "BUDGET_MISMATCH", "Budget belongs to another project or session")
        return budget

    @staticmethod
    def _check(db, run):
        budget = one(db, "SELECT * FROM budgets WHERE id=?", (run["budget_id"],))
        require(budget is not None, "BUDGET_UNAVAILABLE", "Run has no cost budget")
        policy = json.loads(budget["policy_json"])
        started = budget["started_at"] or now_ms()
        elapsed = (now_ms() - started) / 1000
        require(elapsed < policy["max_elapsed_seconds"],
                "BUDGET_EXHAUSTED", "Work elapsed-time budget is exhausted", run_id=run["id"],
                evidence_ref={"budget_id": budget["id"]}, details={"dimension": "elapsed_seconds", "remaining": max(0, policy["max_elapsed_seconds"] - elapsed)})
        if budget["started_at"] is None:
            db.execute("UPDATE budgets SET started_at=? WHERE id=?", (started, budget["id"]))
        return budget, policy

    @staticmethod
    def totals(db, budget_id):
        return one(db, """SELECT COUNT(*) AS calls,COALESCE(SUM(u.input_tokens),0) AS input,
            COALESCE(SUM(u.output_tokens),0) AS output,
            COALESCE(SUM(COALESCE(json_extract(m.normalized_json,'$.input_uncached'),u.input_tokens)),0) AS uncached,
            COALESCE(SUM(m.cost_micros),0) AS cost,
            COALESCE(SUM(CASE WHEN m.budget_phase='finalizing' THEN u.output_tokens ELSE 0 END),0) AS finalization_output,
            COALESCE(SUM(CASE WHEN m.budget_phase='recovery' THEN u.output_tokens ELSE 0 END),0) AS recovery_output,
            COALESCE(SUM(CASE WHEN m.budget_phase='finalizing' THEN m.cost_micros ELSE 0 END),0) AS finalization_cost,
            COALESCE(SUM(CASE WHEN m.budget_phase='recovery' THEN m.cost_micros ELSE 0 END),0) AS recovery_cost,
            COUNT(CASE WHEN m.cost_micros IS NULL THEN 1 END) AS unknown_cost
            FROM usage u LEFT JOIN usage_measurements m ON m.usage_id=u.id WHERE u.budget_id=? AND u.kind='model'""", (budget_id,))

    @staticmethod
    def tails(policy, used):
        result = {}
        for phase in ("finalization", "recovery"):
            fraction = Decimal(str(policy[phase + "_fraction"]))
            result[phase + "_output"] = max(0, int(policy["max_output_tokens"] * fraction) - used[phase + "_output"])
            result[phase + "_cost"] = max(0, int(micros(policy["max_cost"]) * fraction) - used[phase + "_cost"]) if policy["max_cost"] is not None else 0
        return result

    @staticmethod
    def headroom(db, run, requested, *, purpose="model"):
        budget = one(db, "SELECT * FROM budgets WHERE id=?", (run["budget_id"],))
        require(budget is not None, "BUDGET_UNAVAILABLE", "Run has no cost budget")
        policy, used = json.loads(budget["policy_json"]), Budgets.totals(db, budget["id"])
        tails = Budgets.tails(policy, used)
        final, recovery = tails["finalization_output"], tails["recovery_output"]
        remaining = max(0, policy["max_output_tokens"] - used["output"])
        finishing = (budget["phase"] == "finalizing" or remaining - requested < final + recovery
            or policy["max_model_calls"] is not None and policy["max_model_calls"] - used["calls"] <= 2)
        if policy["max_cost"] is not None:
            finishing |= used["cost"] >= micros(policy["max_cost"]) - tails["finalization_cost"] - tails["recovery_cost"]
        # Recovery may consume its own tail; final delivery keeps the remainder.
        reserved = final if purpose in {"summary", "recovery"} else recovery if finishing else final + recovery
        available = max(0, remaining - reserved)
        hint = {"budget_id": budget["id"], "phase": "finalizing" if finishing else "exploring",
                "remaining_output_tokens": remaining, "finalization_reserve_tokens": final,
                "recovery_reserve_tokens": recovery, "finalization_reserve_micros": tails["finalization_cost"],
                "recovery_reserve_micros": tails["recovery_cost"], "basis": "shared_budget",
                "instruction": "Stop expanding the work; verify and deliver existing results, state unfinished items, and use original receipts for recovery." if finishing else ""}
        activity = one(db, "SELECT stalled_model_calls FROM run_activity WHERE run_id=?", (run["id"],))
        if activity and activity["stalled_model_calls"] >= 6:
            hint["progress_notice"] = "Recent calls have not added successful evidence. Reassess the method, inspect the original error, and retain completed work; repeated activity alone is not progress."
        return min(requested, available), hint

    @staticmethod
    def output_allowance(db, run, requested):
        return Budgets.headroom(db, run, requested, purpose="summary")[0]

    @staticmethod
    def reject_before_send(db, identity, *, provider_rejected=False):
        # Measured local accounting zero is not provider-reported usage.
        db.execute("UPDATE usage SET state='settled',input_tokens=0,output_tokens=0,estimated=1 WHERE id=?", (identity,))
        normalized = {"revision": "usage-v3.1", "source": "provider_rejection" if provider_rejected else "local_rejection", "input_total": 0, "input_uncached": 0,
                      "cache_read": 0, "cache_write": 0, "output_total": 0, "reasoning": None}
        db.execute("UPDATE usage_measurements SET normalized_json=?,cost_micros=0,cost_state='settled',updated_at=? WHERE usage_id=?",
                   (encode(normalized), now_ms(), identity))

    def model(self, db, run, identity, purpose, input_tokens, output_tokens, *, model=None, prepared_phase=None):
        previous = one(db, "SELECT * FROM usage WHERE id=?", (identity,))
        if previous:
            return previous["output_tokens"]
        model = model or Model()
        budget, policy = self._check(db, run)
        usage = self.totals(db, budget["id"])
        for dimension, used, requested in (("model_calls", usage["calls"], 1), ("total_input_tokens", usage["input"], input_tokens),
                                            ("uncached_input_tokens", usage["uncached"], input_tokens)):
            limit = policy["max_" + dimension]
            require(limit is None or used + requested <= limit, "BUDGET_EXHAUSTED", "Work " + dimension + " budget is exhausted",
                    evidence_ref={"budget_id": budget["id"]}, details={"dimension": dimension, "remaining": max(0, limit - used) if limit is not None else None, "requested": requested})
        tails = self.tails(policy, usage)
        phase = "recovery" if purpose == "summary" or prepared_phase == "recovery" else (
            "finalizing" if prepared_phase == "finalizing" or budget["phase"] == "finalizing" else "exploring")
        if phase == "exploring" and usage["output"] + output_tokens > policy["max_output_tokens"] - tails["finalization_output"] - tails["recovery_output"]:
            phase = "finalizing"
        remaining = min(output_tokens, policy["max_output_tokens"] - usage["output"])
        require(remaining > 0, "BUDGET_EXHAUSTED", "Work output-token budget is exhausted", evidence_ref={"budget_id": budget["id"]}, details={"dimension": "output_tokens", "remaining": max(0, remaining)})
        normalized = {"profile": model.usage_format}
        if policy["max_cost"] is not None:
            require(model.pricing is not None and model.pricing.currency == policy["currency"] and not usage["unknown_cost"],
                    "PRICE_UNAVAILABLE", "Cost accounting requires known price bounds in the budget currency")
            rate = upper_input_rate(model.pricing, model.usage_format)
            require(rate is not None, "PRICE_UNAVAILABLE", "Configure cache price bounds before using a cost ceiling")
            if phase == "exploring" and usage["cost"] + rate * input_tokens + model.pricing.output_per_million * remaining > micros(policy["max_cost"]) - tails["finalization_cost"] - tails["recovery_cost"]:
                phase = "finalizing"
        # A request built for exploration must receive the finalization guidance
        # before it can draw on that reserve. This is a local preparation change,
        # not a provider retry; no usage/action is created on this path.
        require(not (phase == "finalizing" and prepared_phase not in {None, "finalizing"}),
                "BUDGET_FINALIZATION_REQUIRED", "Prepare finalization guidance before using the reserved budget",
                details={"budget_id": budget["id"], "request_sent": False})
        protected_output = tails["finalization_output"] if phase == "recovery" else tails["recovery_output"] if phase == "finalizing" else tails["finalization_output"] + tails["recovery_output"]
        remaining = min(remaining, max(0, policy["max_output_tokens"] - usage["output"] - protected_output))
        require(remaining > 0, "BUDGET_EXHAUSTED", "The available output budget is exhausted; the remaining reserve belongs to another purpose")
        if policy["max_cost"] is not None:
            protected_cost = tails["finalization_cost"] if phase == "recovery" else tails["recovery_cost"] if phase == "finalizing" else tails["finalization_cost"] + tails["recovery_cost"]
            available = micros(policy["max_cost"]) - usage["cost"] - rate * input_tokens - protected_cost
            if model.pricing.output_per_million:
                remaining = min(remaining, max(0, int(available / model.pricing.output_per_million)))
            require(remaining > 0 and available >= 0, "BUDGET_EXHAUSTED", "Work cost budget cannot fund another request")
        cost, cost_state = charge(normalized, model.pricing, estimated_input=input_tokens, estimated_output=remaining)
        if policy["max_cost"] is not None:
            require(model.pricing is not None and model.pricing.currency == policy["currency"] and cost is not None and not usage["unknown_cost"],
                    "PRICE_UNAVAILABLE", "A cost ceiling requires a price snapshot and explicit cache price bounds in the budget currency")
            require(usage["cost"] + cost <= micros(policy["max_cost"]), "BUDGET_EXHAUSTED", "Work cost budget is exhausted",
                    evidence_ref={"budget_id": budget["id"]}, details={"dimension": "cost", "currency": policy["currency"],
                    "remaining_micros": max(0, micros(policy["max_cost"]) - usage["cost"]), "requested_micros": cost})
        activity = one(db, "SELECT * FROM run_activity WHERE run_id=?", (run["id"],))
        blocker = json.loads(activity["blocker_json"]) if activity and activity["blocker_json"] else {}
        limit = policy["max_stalled_model_calls"]
        require(limit is None or purpose == "summary" or not activity or activity["stalled_model_calls"] < limit,
                "NO_PROGRESS", "Work has no new successful action or delivery evidence", cause=blocker or None,
                evidence_ref=blocker.get("evidence_ref"), details={"repeated": activity["blocker_repeats"] if activity else 0})
        db.execute("INSERT INTO usage VALUES(?,?,?,?,?,'model','reserved',?,NULL,?,1,?)",
                   (identity, budget["id"], run["id"], None, purpose, input_tokens, remaining, now_ms()))
        db.execute("INSERT INTO usage_measurements(usage_id,profile,price_json,cost_micros,cost_state,updated_at,budget_phase) VALUES(?,?,?,?,?,?,?)",
                   (identity, model.usage_format, encode(model.pricing) if model.pricing else None, cost, "reserved" if cost is not None else cost_state, now_ms(), phase))
        if phase == "finalizing":
            db.execute("UPDATE budgets SET phase='finalizing' WHERE id=?", (budget["id"],))
        if purpose != "summary":
            db.execute("INSERT INTO run_activity(run_id,stalled_model_calls) VALUES(?,1) ON CONFLICT(run_id) DO UPDATE SET stalled_model_calls=stalled_model_calls+1", (run["id"],))
        return remaining

    @staticmethod
    def settle(db, identity, usage):
        previous = one(db, "SELECT u.*,m.profile,m.raw_json,m.normalized_json,m.price_json,m.revision FROM usage u LEFT JOIN usage_measurements m ON m.usage_id=u.id WHERE u.id=?", (identity,))
        if previous is None:
            return
        if previous["raw_json"] == encode(usage):
            return json.loads(previous["normalized_json"]) if previous["normalized_json"] else None
        normalized = normalize(usage, previous["profile"] or "openai")
        incoming, outgoing = normalized["input_total"], normalized["output_total"]
        known = incoming is not None and outgoing is not None
        price = json.loads(previous["price_json"]) if previous["price_json"] else None
        cost, cost_state = charge(normalized, price, estimated_input=previous["input_tokens"], estimated_output=previous["output_tokens"])
        db.execute("UPDATE usage SET state=?,input_tokens=?,cached_input_tokens=?,output_tokens=?,estimated=? WHERE id=?",
                   ("settled" if known else "needs_review", incoming if incoming is not None else previous["input_tokens"], normalized["cache_read"],
                    outgoing if outgoing is not None else previous["output_tokens"], int(not known), identity))
        revision = (previous["revision"] or 0) + 1
        db.execute("INSERT INTO usage_measurements(usage_id,profile,raw_json,normalized_json,price_json,cost_micros,cost_state,revision,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(usage_id) DO UPDATE SET raw_json=excluded.raw_json,normalized_json=excluded.normalized_json,cost_micros=excluded.cost_micros,cost_state=excluded.cost_state,revision=excluded.revision,updated_at=excluded.updated_at",
                   (identity, normalized["profile"], encode(usage), encode(normalized), previous["price_json"], cost, cost_state, revision, now_ms()))
        if previous["raw_json"] is not None:
            event(db, "usage.corrected", run_id=previous["run_id"], data={"usage_id": identity, "revision": revision})
        return normalized

    def tool(self, db, run, identity, name):
        budget, policy = self._check(db, run)
        count = one(db, "SELECT COUNT(*) AS count FROM usage WHERE budget_id=? AND kind='tool'", (budget["id"],))["count"]
        require(policy["max_tool_calls"] is None or count < policy["max_tool_calls"], "BUDGET_EXHAUSTED", "Work tool-call budget is exhausted", evidence_ref={"budget_id": budget["id"]}, details={"dimension": "tool_calls", "remaining": max(0, policy["max_tool_calls"] - count) if policy["max_tool_calls"] is not None else None, "requested": 1})
        db.execute("INSERT INTO usage VALUES(?,?,?,?,?,'tool','settled',0,0,0,0,?)",
                   (identity, budget["id"], run["id"], identity, name, now_ms()))

    @staticmethod
    def progress(db, run_id, source, value):
        if source == "wait_for_input" or value.get("isError") or value.get("truncated"):
            return
        evidence = value
        if source in {"exec_command", "write_stdin"}:
            if value.get("state") == "running":
                return
            evidence = {key: value.get(key) for key in ("exit_code", "stdout", "stderr")}
            evidence["output_refs"] = [{key: reference.get(key) for key in ("stream", "saved_bytes", "observed_bytes", "truncated")}
                                       for reference in value.get("output_refs", [])]
        fingerprint = hashlib.sha256(encode({"source": source, "evidence": evidence}).encode("utf-8")).hexdigest()
        db.execute("INSERT OR IGNORE INTO run_progress VALUES(?,?,?,?)", (run_id, fingerprint, source, now_ms()))
        if db.changes():
            db.execute("INSERT INTO run_activity(run_id,stalled_model_calls) VALUES(?,0) ON CONFLICT(run_id) DO UPDATE SET stalled_model_calls=0,blocker_json=NULL,blocker_signature=NULL,blocker_repeats=0", (run_id,))

    @staticmethod
    def blocked(db, run_id, error, signature):
        db.execute("INSERT INTO run_activity(run_id,blocker_json,blocker_signature,blocker_repeats) VALUES(?,?,?,1) "
                   "ON CONFLICT(run_id) DO UPDATE SET blocker_json=excluded.blocker_json,"
                   "blocker_repeats=CASE WHEN blocker_signature=excluded.blocker_signature THEN blocker_repeats+1 ELSE 1 END,"
                   "blocker_signature=excluded.blocker_signature", (run_id, encode(error), signature))

    @staticmethod
    def annotate_stop(db, run_id, fault):
        if fault.code in {"BUDGET_EXHAUSTED", "EXECUTION_STEP_LIMIT"}:
            activity = one(db, "SELECT blocker_json,blocker_repeats FROM run_activity WHERE run_id=?", (run_id,))
            if activity and activity["blocker_json"]:
                fault.identity["cause"] = json.loads(activity["blocker_json"])
                fault.details["blocker_repeats"] = activity["blocker_repeats"]

    @staticmethod
    def read(db, budget_id, *, scope="shared_budget", run=None):
        budget = one(db, "SELECT * FROM budgets WHERE id=?", (budget_id,))
        require(budget is not None, "NOT_FOUND", "Budget does not exist")
        require(scope in {"run_direct", "root_work", "shared_budget"}, "INVALID_REQUEST", "Unknown usage scope")
        where, parameters = "u.budget_id=?", [budget_id]
        if scope != "shared_budget":
            require(run is not None and run["budget_id"] == budget_id, "PERMISSION_DENIED", "Usage scope needs its original run")
            if scope == "run_direct":
                where += " AND u.run_id=?"
                parameters.append(run["id"])
            else:
                root = "root_run_id" if run["root_run_id"] else "root_workflow_run_id"
                where += " AND u.run_id IN (SELECT id FROM runs WHERE " + root + "=?)"
                parameters.append(run[root])
        entries = rows(db, f"""WITH evidence AS (
            SELECT u.*,m.normalized_json,m.price_json,m.cost_micros,m.cost_state,m.budget_phase,m.revision AS measurement_revision,m.updated_at AS measured_at,COALESCE(f.input_json,a.input_json) AS metadata,
            CASE WHEN f.id IS NOT NULL THEN f.result_json ELSE a.result_json END AS receipt
            FROM usage u LEFT JOIN usage_measurements m ON m.usage_id=u.id LEFT JOIN actions a ON a.id=u.action_id LEFT JOIN model_failures f ON f.id=u.id WHERE {where}
        ), facts AS (
            SELECT *,CASE WHEN normalized_json IS NOT NULL THEN CASE WHEN json_extract(normalized_json,'$.source') IN ('provider','historical_provider') THEN json_extract(normalized_json,'$.input_total') END ELSE COALESCE(json_extract(receipt,'$.usage.prompt_tokens'),json_extract(receipt,'$.usage.input_tokens')) END AS provider_input,
            CASE WHEN normalized_json IS NOT NULL THEN CASE WHEN json_extract(normalized_json,'$.source') IN ('provider','historical_provider') THEN json_extract(normalized_json,'$.output_total') END ELSE COALESCE(json_extract(receipt,'$.usage.completion_tokens'),json_extract(receipt,'$.usage.output_tokens')) END AS provider_output,
            CASE WHEN normalized_json IS NOT NULL THEN json_extract(normalized_json,'$.cache_read') ELSE COALESCE(json_extract(receipt,'$.usage.prompt_tokens_details.cached_tokens'),json_extract(receipt,'$.usage.input_tokens_details.cached_tokens'),json_extract(receipt,'$.usage.cached_input_tokens')) END AS provider_cache,
            json_extract(metadata,'$.attempt.send_started') AS sent,
            json_extract(metadata,'$.attempt.response_received') AS responded,
            json_extract(metadata,'$.attempt.generation_completed') AS generated,
            json_extract(metadata,'$.attempt.outcome') AS outcome,
            json_extract(metadata,'$.attempt.attempt_id') AS attempt_id
            FROM evidence
        ) SELECT kind,purpose,state,estimated,COUNT(*) AS calls,COUNT(cached_input_tokens) AS cache_reports,
            json_extract(price_json,'$.currency') AS price_currency,json_extract(price_json,'$.revision') AS price_revision,
            SUM(cost_micros) AS cost_micros,COUNT(cost_micros) AS cost_reports,
            SUM(CASE WHEN budget_phase='finalizing' THEN output_tokens ELSE 0 END) AS finalization_output,
            SUM(CASE WHEN budget_phase='recovery' THEN output_tokens ELSE 0 END) AS recovery_output,
            SUM(CASE WHEN budget_phase='finalizing' THEN cost_micros ELSE 0 END) AS finalization_cost,
            SUM(CASE WHEN budget_phase='recovery' THEN cost_micros ELSE 0 END) AS recovery_cost,
            SUM(CASE WHEN state='settled' AND cost_state='settled' THEN cost_micros ELSE 0 END) AS settled_cost_micros,
            SUM(CASE WHEN state='settled' AND cost_state='estimated' THEN cost_micros ELSE 0 END) AS estimated_cost_micros,
            MAX(measurement_revision) AS measurement_revision,MAX(measured_at) AS measured_at,
            SUM(json_extract(normalized_json,'$.input_uncached')) AS uncached_tokens,
            SUM(COALESCE(json_extract(normalized_json,'$.input_uncached'),input_tokens)) AS accounted_uncached_tokens,
            COUNT(json_extract(normalized_json,'$.input_uncached')) AS uncached_reports,
            SUM(json_extract(normalized_json,'$.cache_write')) AS cache_write_tokens,
            COUNT(json_extract(normalized_json,'$.cache_write')) AS cache_write_reports,
            SUM(json_extract(normalized_json,'$.reasoning')) AS reasoning_tokens,
            COUNT(json_extract(normalized_json,'$.reasoning')) AS reasoning_reports,
            SUM(input_tokens) AS input_tokens,SUM(cached_input_tokens) AS cached_input_tokens,SUM(output_tokens) AS output_tokens,
            COUNT(CASE WHEN typeof(provider_input)='integer' AND provider_input>=0 THEN 1 END) AS input_reports,
            SUM(CASE WHEN typeof(provider_input)='integer' AND provider_input>=0 THEN provider_input END) AS reported_input_tokens,
            COUNT(CASE WHEN typeof(provider_output)='integer' AND provider_output>=0 THEN 1 END) AS output_reports,
            SUM(CASE WHEN typeof(provider_output)='integer' AND provider_output>=0 THEN provider_output END) AS reported_output_tokens,
            COUNT(CASE WHEN typeof(provider_cache)='integer' AND provider_cache>=0 AND typeof(provider_input)='integer' AND provider_input>=0 AND provider_cache<=provider_input THEN 1 END) AS reported_cache_calls,
            SUM(CASE WHEN typeof(provider_cache)='integer' AND provider_cache>=0 AND typeof(provider_input)='integer' AND provider_input>=0 AND provider_cache<=provider_input THEN provider_cache END) AS reported_cache_tokens,
            SUM(CASE WHEN json_extract(normalized_json,'$.invalid_fields') LIKE '%cache_exceeds_input%' OR (typeof(provider_cache)='integer' AND typeof(provider_input)='integer' AND provider_input>=0 AND provider_cache>provider_input) THEN 1 ELSE 0 END) AS invalid_cache_reports,
            SUM(CASE WHEN outcome='failed' AND sent=0 THEN 1 ELSE 0 END) AS rejected_before_send,
            COUNT(sent) AS send_coverage,COUNT(responded) AS response_coverage,COUNT(outcome) AS terminal_coverage,
            SUM(CASE WHEN responded=1 THEN 1 ELSE 0 END) AS responses_received,
            SUM(CASE WHEN generated=1 THEN 1 ELSE 0 END) AS generation_completed,
            SUM(CASE WHEN outcome='failed' THEN 1 ELSE 0 END) AS failed,
            COUNT(attempt_id) AS known_attempts
            FROM facts GROUP BY kind,purpose,state,estimated,price_currency,price_revision""", parameters)
        policy = json.loads(budget["policy_json"])
        models = [entry for entry in entries if entry["kind"] == "model"]
        usage = {"model_calls": sum(entry["calls"] for entry in models), "tool_calls": sum(entry["calls"] for entry in entries if entry["kind"] == "tool"),
                 "input_tokens": sum(entry["input_tokens"] for entry in models), "output_tokens": sum(entry["output_tokens"] for entry in models)}
        reported_cache = sum(entry["cached_input_tokens"] or 0 for entry in models)
        usage["cached_input_tokens"] = reported_cache if all(entry["cache_reports"] == entry["calls"] for entry in models) else None
        for metric, column, coverage in (("input_uncached", "uncached_tokens", "uncached_reports"), ("cache_write", "cache_write_tokens", "cache_write_reports"), ("reasoning", "reasoning_tokens", "reasoning_reports")):
            usage[metric] = sum(entry[column] or 0 for entry in models) if all(entry[coverage] == entry["calls"] for entry in models) else None
        usage.update(input_total=usage["input_tokens"], output_total=usage["output_tokens"], cache_read=usage["cached_input_tokens"])
        usage["accounted_uncached_tokens"] = sum(entry["accounted_uncached_tokens"] or 0 for entry in models)
        currencies = {}
        for entry in models:
            currency = entry["price_currency"]
            if currency is None:
                continue
            amount = currencies.setdefault(currency, {"accounted_micros": 0, "settled_micros": 0, "estimated_micros": 0, "reserved_micros": 0, "pending_micros": 0, "price_revisions": []})
            amount["accounted_micros"] += entry["cost_micros"] or 0
            amount["settled_micros"] += entry["settled_cost_micros"] or 0
            amount["estimated_micros"] += entry["estimated_cost_micros"] or 0
            if entry["state"] in {"reserved", "needs_review"}:
                amount["reserved_micros" if entry["state"] == "reserved" else "pending_micros"] += entry["cost_micros"] or 0
            if entry["price_revision"] not in amount["price_revisions"]:
                amount["price_revisions"].append(entry["price_revision"])
        cost = {"currencies": currencies, "unknown_calls": sum(entry["calls"] - entry["cost_reports"] for entry in models),
                "unit": "micro_currency", "rounding": "ceiling_per_attempt", "revision": "cost-v3.1"}
        def reported(metric, coverage):
            count = sum(entry[coverage] for entry in models)
            return sum(entry[metric] or 0 for entry in models) if count else None
        provider = {"basis": "provider_reported", "input_tokens": reported("reported_input_tokens", "input_reports"),
                    "output_tokens": reported("reported_output_tokens", "output_reports"), "cached_input_tokens": reported("reported_cache_tokens", "reported_cache_calls"),
                    "coverage": {"input_calls": sum(entry["input_reports"] for entry in models), "output_calls": sum(entry["output_reports"] for entry in models),
                                 "cache_calls": sum(entry["reported_cache_calls"] for entry in models)},
                    "invalid_cache_reports": sum(entry["invalid_cache_reports"] for entry in models)}
        def bucket(state, estimated=None):
            values = [entry for entry in models if entry["state"] == state and (estimated is None or entry["estimated"] == estimated)]
            return {"input_tokens": sum(entry["input_tokens"] for entry in values), "output_tokens": sum(entry["output_tokens"] for entry in values),
                    "model_calls": sum(entry["calls"] - entry["rejected_before_send"] for entry in values)}
        estimated = bucket("settled", 1)
        pending = bucket("needs_review")
        attempts = {"total": usage["model_calls"], **{key: sum(entry[key] for entry in models) for key in
                    ("rejected_before_send", "responses_received", "generation_completed", "failed", "known_attempts", "send_coverage", "response_coverage", "terminal_coverage")}}
        attempts["historical_unknown"] = attempts["total"] - attempts["known_attempts"]
        reserved = {"input_tokens": sum(entry["input_tokens"] for entry in models if entry["state"] == "reserved"),
                    "output_tokens": sum(entry["output_tokens"] for entry in models if entry["state"] == "reserved")}
        tail_usage = {name: sum(entry[name] or 0 for entry in models) for name in ("finalization_output", "recovery_output", "finalization_cost", "recovery_cost")}
        tails = Budgets.tails(policy, tail_usage)
        remaining_cost = max(0, micros(policy["max_cost"]) - currencies.get(policy["currency"], {}).get("accounted_micros", 0)) if policy["max_cost"] is not None and not cost["unknown_calls"] else None
        return {"id": budget["id"], "project_id": budget["project_id"], "session_id": budget["session_id"], "root_run_id": budget["root_run_id"], "root_workflow_run_id": budget["root_workflow_run_id"], "started_at": budget["started_at"],
                "scope": scope, "includes_children": scope != "run_direct", "as_of": now_ms(), "phase": budget["phase"], "cost": cost,
                "policy": policy, "usage": usage, "provider_reported": provider, "estimated": estimated, "reserved": reserved, "needs_review": pending, "attempts": attempts,
                "reserves": {"currency": policy["currency"], "included_in_original_budget": True,
                             **{phase: {"output_tokens": tails[phase + "_output"], "cost_micros": tails[phase + "_cost"] if policy["max_cost"] is not None else None}
                                for phase in ("finalization", "recovery")}} if scope == "shared_budget" else None,
                "remaining": ({"cost_micros": remaining_cost, **{key: max(0, policy[limit] - (usage.get(metric) or 0)) if policy.get(limit) is not None else None
                    for key, metric, limit in (("model_calls", "model_calls", "max_model_calls"), ("tool_calls", "tool_calls", "max_tool_calls"),
                    ("total_input_tokens", "input_tokens", "max_total_input_tokens"), ("uncached_input_tokens", "accounted_uncached_tokens", "max_uncached_input_tokens"),
                    ("output_tokens", "output_tokens", "max_output_tokens"))}} if scope == "shared_budget" else None),
                "estimated_model_calls": estimated["model_calls"], "breakdown": entries}

    async def get(self, budget_id: str):
        return await self.store.call(lambda db: self.read(db, budget_id))

    def scopes(self, db, run):
        return {scope: self.read(db, run["budget_id"], scope=scope, run=run) for scope in ("run_direct", "root_work", "shared_budget")}

    async def for_run(self, run):
        return await self.store.call(lambda db: self.scopes(db, run), transaction="read")
