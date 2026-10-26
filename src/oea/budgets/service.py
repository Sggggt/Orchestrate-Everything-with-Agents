"""Intent-owned usage accounting; never an execution admission mechanism."""

import hashlib
import json

from oea.contracts.configuration import Budget
from oea.catalog.service import need
from oea.contracts.errors import require
from oea.storage.persistence import encode, new_id, now_ms, one, rows


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
        require(now_ms() - started < policy["max_elapsed_seconds"] * 1000,
                "BUDGET_EXHAUSTED", "Work elapsed-time budget is exhausted", run_id=run["id"])
        if budget["started_at"] is None:
            db.execute("UPDATE budgets SET started_at=? WHERE id=?", (started, budget["id"]))
        return budget, policy

    @staticmethod
    def output_allowance(db, run, requested):
        row = one(db, "SELECT json_extract(b.policy_json,'$.max_output_tokens')-COALESCE(SUM(u.output_tokens),0) AS remaining FROM budgets b LEFT JOIN usage u ON u.budget_id=b.id AND u.kind='model' WHERE b.id=? GROUP BY b.id", (run["budget_id"],))
        require(row is not None, "BUDGET_UNAVAILABLE", "Run has no cost budget")
        return max(0, min(requested, row["remaining"]))

    @staticmethod
    def reject_before_send(db, identity):
        # Measured local accounting zero is not provider-reported usage.
        db.execute("UPDATE usage SET state='settled',input_tokens=0,output_tokens=0,estimated=1 WHERE id=?", (identity,))

    def model(self, db, run, identity, purpose, input_tokens, output_tokens):
        previous = one(db, "SELECT * FROM usage WHERE id=?", (identity,))
        if previous:
            return previous["output_tokens"]
        budget, policy = self._check(db, run)
        usage = one(db, "SELECT COUNT(*) AS calls,COALESCE(SUM(input_tokens),0) AS input,COALESCE(SUM(output_tokens),0) AS output FROM usage WHERE budget_id=? AND kind='model'", (budget["id"],))
        require(usage["calls"] < policy["max_model_calls"], "BUDGET_EXHAUSTED", "Work model-call budget is exhausted")
        require(usage["input"] + input_tokens <= policy["max_input_tokens"], "BUDGET_EXHAUSTED", "Work input-token budget is exhausted")
        remaining = min(output_tokens, policy["max_output_tokens"] - usage["output"])
        require(remaining > 0, "BUDGET_EXHAUSTED", "Work output-token budget is exhausted")
        activity = one(db, "SELECT stalled_model_calls FROM run_activity WHERE run_id=?", (run["id"],))
        require(purpose == "summary" or not activity or activity["stalled_model_calls"] < policy["max_stalled_model_calls"],
                "NO_PROGRESS", "Work has no new successful action or delivery evidence")
        db.execute("INSERT INTO usage VALUES(?,?,?,?,?,'model','reserved',?,NULL,?,1,?)",
                   (identity, budget["id"], run["id"], None, purpose, input_tokens, remaining, now_ms()))
        if purpose != "summary":
            db.execute("INSERT INTO run_activity VALUES(?,1) ON CONFLICT(run_id) DO UPDATE SET stalled_model_calls=stalled_model_calls+1", (run["id"],))
        return remaining

    @staticmethod
    def settle(db, identity, usage):
        incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
        outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
        known = type(incoming) is int and incoming >= 0 and type(outgoing) is int and outgoing >= 0
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else usage.get("cached_input_tokens")
        cached = cached if type(cached) is int and cached >= 0 else None
        if known:
            db.execute("UPDATE usage SET state='settled',input_tokens=?,cached_input_tokens=?,output_tokens=?,estimated=0 WHERE id=?",
                       (incoming, min(cached, incoming) if cached is not None else None, outgoing, identity))
        else:
            db.execute("UPDATE usage SET state='settled',estimated=1 WHERE id=?", (identity,))

    def tool(self, db, run, identity, name):
        budget, policy = self._check(db, run)
        count = one(db, "SELECT COUNT(*) AS count FROM usage WHERE budget_id=? AND kind='tool'", (budget["id"],))["count"]
        require(count < policy["max_tool_calls"], "BUDGET_EXHAUSTED", "Work tool-call budget is exhausted")
        db.execute("INSERT INTO usage VALUES(?,?,?,?,?,'tool','settled',0,0,0,0,?)",
                   (identity, budget["id"], run["id"], identity, name, now_ms()))

    @staticmethod
    def progress(db, run_id, source, value):
        if source == "wait_for_input" or value.get("isError") or value.get("truncated"):
            return
        evidence = value
        if source == "run_command":
            evidence = {key: value.get(key) for key in ("exit_code", "stdout", "stderr")}
            evidence["output_hashes"] = [item["artifact"].get("sha256") for item in value.get("output_refs", [])]
        fingerprint = hashlib.sha256(encode({"source": source, "evidence": evidence}).encode("utf-8")).hexdigest()
        db.execute("INSERT OR IGNORE INTO run_progress VALUES(?,?,?,?)", (run_id, fingerprint, source, now_ms()))
        if db.changes():
            db.execute("INSERT INTO run_activity VALUES(?,0) ON CONFLICT(run_id) DO UPDATE SET stalled_model_calls=0", (run_id,))

    async def get(self, budget_id: str):
        def read(db):
            budget = one(db, "SELECT * FROM budgets WHERE id=?", (budget_id,))
            require(budget is not None, "NOT_FOUND", "Budget does not exist")
            entries = rows(db, """WITH evidence AS (
                SELECT u.*,COALESCE(f.input_json,a.input_json) AS metadata,
                CASE WHEN f.id IS NOT NULL THEN f.result_json ELSE a.result_json END AS receipt
                FROM usage u LEFT JOIN actions a ON a.id=u.action_id LEFT JOIN model_failures f ON f.id=u.id WHERE u.budget_id=?
            ), facts AS (
                SELECT *,COALESCE(json_extract(receipt,'$.usage.prompt_tokens'),json_extract(receipt,'$.usage.input_tokens')) AS provider_input,
                COALESCE(json_extract(receipt,'$.usage.completion_tokens'),json_extract(receipt,'$.usage.output_tokens')) AS provider_output,
                COALESCE(json_extract(receipt,'$.usage.prompt_tokens_details.cached_tokens'),json_extract(receipt,'$.usage.input_tokens_details.cached_tokens'),json_extract(receipt,'$.usage.cached_input_tokens')) AS provider_cache,
                json_extract(metadata,'$.attempt.send_started') AS sent,
                json_extract(metadata,'$.attempt.response_received') AS responded,
                json_extract(metadata,'$.attempt.generation_completed') AS generated,
                json_extract(metadata,'$.attempt.outcome') AS outcome,
                json_extract(metadata,'$.attempt.attempt_id') AS attempt_id
                FROM evidence
            ) SELECT kind,purpose,state,estimated,COUNT(*) AS calls,COUNT(cached_input_tokens) AS cache_reports,
                SUM(input_tokens) AS input_tokens,SUM(cached_input_tokens) AS cached_input_tokens,SUM(output_tokens) AS output_tokens,
                COUNT(CASE WHEN typeof(provider_input)='integer' AND provider_input>=0 THEN 1 END) AS input_reports,
                SUM(CASE WHEN typeof(provider_input)='integer' AND provider_input>=0 THEN provider_input END) AS reported_input_tokens,
                COUNT(CASE WHEN typeof(provider_output)='integer' AND provider_output>=0 THEN 1 END) AS output_reports,
                SUM(CASE WHEN typeof(provider_output)='integer' AND provider_output>=0 THEN provider_output END) AS reported_output_tokens,
                COUNT(CASE WHEN typeof(provider_cache)='integer' AND provider_cache>=0 AND typeof(provider_input)='integer' AND provider_input>=0 AND provider_cache<=provider_input THEN 1 END) AS reported_cache_calls,
                SUM(CASE WHEN typeof(provider_cache)='integer' AND provider_cache>=0 AND typeof(provider_input)='integer' AND provider_input>=0 AND provider_cache<=provider_input THEN provider_cache END) AS reported_cache_tokens,
                SUM(CASE WHEN typeof(provider_cache)='integer' AND typeof(provider_input)='integer' AND provider_input>=0 AND provider_cache>provider_input THEN 1 ELSE 0 END) AS invalid_cache_reports,
                SUM(CASE WHEN outcome='failed' AND sent=0 THEN 1 ELSE 0 END) AS rejected_before_send,
                COUNT(sent) AS send_coverage,COUNT(responded) AS response_coverage,COUNT(outcome) AS terminal_coverage,
                SUM(CASE WHEN responded=1 THEN 1 ELSE 0 END) AS responses_received,
                SUM(CASE WHEN generated=1 THEN 1 ELSE 0 END) AS generation_completed,
                SUM(CASE WHEN outcome='failed' THEN 1 ELSE 0 END) AS failed,
                COUNT(attempt_id) AS known_attempts
                FROM facts GROUP BY kind,purpose,state,estimated""", (budget_id,))
            policy = json.loads(budget["policy_json"])
            models = [entry for entry in entries if entry["kind"] == "model"]
            usage = {"model_calls": sum(entry["calls"] for entry in models), "tool_calls": sum(entry["calls"] for entry in entries if entry["kind"] == "tool"),
                     "input_tokens": sum(entry["input_tokens"] for entry in models), "output_tokens": sum(entry["output_tokens"] for entry in models)}
            reported_cache = sum(entry["cached_input_tokens"] or 0 for entry in models)
            usage["cached_input_tokens"] = reported_cache if all(entry["cache_reports"] == entry["calls"] for entry in models) else None
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
            return {"id": budget["id"], "project_id": budget["project_id"], "session_id": budget["session_id"], "root_run_id": budget["root_run_id"], "root_workflow_run_id": budget["root_workflow_run_id"], "started_at": budget["started_at"],
                    "policy": policy, "usage": usage, "provider_reported": provider, "estimated": estimated, "reserved": reserved, "needs_review": pending, "attempts": attempts,
                    "remaining": {key: max(0, policy["max_" + key] - usage[key]) for key in ("model_calls", "tool_calls", "input_tokens", "output_tokens")},
                    "estimated_model_calls": estimated["model_calls"], "breakdown": entries}
        return await self.store.call(read)
