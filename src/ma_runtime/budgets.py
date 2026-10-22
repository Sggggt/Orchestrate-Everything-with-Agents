"""One durable charge per operation; descendants share the original ledger."""

import json

from .errors import RuntimeFault, require
from .models import BudgetPolicy, content_hash, json_dump, new_id, now_ms
from .store import emit, one, rows


class Budgets:
    def __init__(self, runtime):
        self.rt = runtime

    def reject(self, context, error):
        if isinstance(error, RuntimeFault) and error.code in {"BUDGET_EXHAUSTED", "PROGRESS_STALLED"}:
            context.budget_failure = error

    async def open(self):
        """Import existing cost evidence once, in the upgrade transaction."""
        def upgrade(db):
            if one(db, "SELECT applied FROM budget_upgrade WHERE id=1").applied:
                return
            for task in rows(db, "SELECT id FROM tasks ORDER BY created_at,rowid"):
                self.task(db, task.id)
            for run in rows(db, "SELECT id,session_id FROM workflow_runs ORDER BY created_at,rowid"):
                self.workflow(db, run.id, run.session_id)
            for record in rows(db, "SELECT e.task_id,e.run_id,e.payload_json,e.created_at,tb.budget_id FROM events e "
                               "JOIN task_budgets tb ON tb.task_id=e.task_id WHERE e.type='run.stage' ORDER BY e.seq"):
                stage = json.loads(record.payload_json)
                if stage.get("stage") not in {"model", "summary"}:
                    continue
                attributes = stage.get("attributes", {})
                usage = attributes.get("usage") or {}
                incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
                outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
                known = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
                if not known:
                    incoming = attributes.get("input_tokens_estimate") or attributes.get("context", {}).get("input_tokens") or 48000
                    outgoing = attributes.get("max_output_tokens") or 4096
                db.execute("INSERT INTO budget_entries(id,budget_id,task_id,run_id,kind,purpose,input_tokens,output_tokens,estimated,state,created_at) "
                           "VALUES(?,?,?,?,'model',?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                           "input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,estimated=excluded.estimated,state=excluded.state",
                           (stage["id"], record.budget_id, record.task_id, record.run_id, stage["stage"], incoming, outgoing,
                            int(not known), "RESERVED" if stage["state"] == "RUNNING" else "SETTLED", record.created_at))
            db.execute("INSERT INTO budget_entries(id,budget_id,task_id,run_id,kind,purpose,estimated,state,created_at) "
                       "SELECT c.id,tb.budget_id,r.task_id,c.run_id,'tool',c.tool_name,0,'SETTLED',c.created_at "
                       "FROM tool_calls c JOIN runs r ON r.id=c.run_id JOIN task_budgets tb ON tb.task_id=r.task_id")
            for budget in rows(db, "SELECT id FROM execution_budgets"):
                totals = one(db, "SELECT COALESCE(SUM(kind='model'),0) AS models,COALESCE(SUM(input_tokens),0) AS incoming,"
                             "COALESCE(SUM(output_tokens),0) AS outgoing,COALESCE(SUM(kind='tool'),0) AS tools FROM budget_entries WHERE budget_id=?", (budget.id,))
                first = one(db, "SELECT MIN(r.started_at) AS stamp FROM runs r JOIN task_budgets t ON t.task_id=r.task_id WHERE t.budget_id=?", (budget.id,)).stamp
                db.execute("UPDATE execution_budgets SET started_at=?,model_calls=?,input_tokens=?,output_tokens=?,tool_calls=? WHERE id=?",
                           (first, totals.models, totals.incoming, totals.outgoing, totals.tools, budget.id))
            db.execute("UPDATE budget_upgrade SET applied=1 WHERE id=1")
        await self.rt.store.call(upgrade, transaction=True)

    def create_in(self, db, session_id, policy, key):
        digest = content_hash(policy)
        old = one(db, "SELECT * FROM execution_budgets WHERE session_id=? AND idempotency_key=?", (session_id, key))
        if old:
            require(old.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Budget key has different limits")
            return old.id
        identity = new_id("budget")
        db.execute("INSERT INTO execution_budgets(id,session_id,policy_json,idempotency_key,request_hash,created_at) VALUES(?,?,?,?,?,?)",
                   (identity, session_id, json_dump(policy), key, digest, now_ms()))
        emit(db, "budget.created", session_id=session_id, payload={"budget_id": identity})
        return identity

    async def create(self, session_id, policy: BudgetPolicy | dict, idempotency_key):
        policy = BudgetPolicy.model_validate(policy)
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256,
                "INVALID_REQUEST", "A bounded budget key is required")
        from .services import need
        def save(db):
            require(need(db, "sessions", session_id).state == "OPEN", "SESSION_CLOSED", "Budget requires an open session")
            return self.create_in(db, session_id, policy, idempotency_key)
        identity = await self.rt.store.call(save, transaction=True)
        return await self.get(identity)

    async def amend(self, budget_id, policy: BudgetPolicy | dict, expected_policy_hash, idempotency_key, reason):
        """An explicit administrative change preserves the existing execution ledger."""
        policy = BudgetPolicy.model_validate(policy)
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256,
                "INVALID_REQUEST", "A bounded amendment key is required")
        require(isinstance(reason, str) and 8 <= len(reason.strip()) <= 2000,
                "INVALID_REQUEST", "Record a concise reason for changing the limits")
        digest = content_hash({"policy": policy, "expected_policy_hash": expected_policy_hash, "reason": reason})
        from .services import need
        def save(db):
            row = need(db, "execution_budgets", budget_id)
            prior = one(db, "SELECT * FROM budget_amendments WHERE budget_id=? AND idempotency_key=?",
                        (budget_id, idempotency_key))
            if prior:
                require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Amendment key has different parameters")
                return {"budget_id": budget_id, "policy_hash": content_hash(json.loads(prior.policy_json)), "reused": True}
            require(need(db, "sessions", row.session_id).state == "OPEN", "SESSION_CLOSED", "Budget requires an open session")
            previous = json.loads(row.policy_json)
            require(content_hash(previous) == expected_policy_hash, "REVISION_CONFLICT", "Budget policy changed")
            for name in ("model_calls", "input_tokens", "output_tokens", "tool_calls"):
                require(getattr(policy, "max_" + name) >= row[name], "INVALID_REQUEST", "A limit cannot be below already charged usage",
                        details={"limit": "max_" + name, "used": row[name]})
            stamp = now_ms()
            encoded = json_dump(policy)
            db.execute("INSERT INTO budget_amendments VALUES(?,?,?,?,?,?,?)",
                       (budget_id, idempotency_key, digest, row.policy_json, encoded, reason, stamp))
            db.execute("UPDATE execution_budgets SET policy_json=? WHERE id=?", (encoded, budget_id))
            emit(db, "budget.amended", session_id=row.session_id,
                 payload={"budget_id": budget_id, "previous_policy": previous, "policy": policy.model_dump(mode="json"), "reason": reason})
            return {"budget_id": budget_id, "policy_hash": content_hash(policy), "reused": False}
        result = await self.rt.store.call(save, transaction=True)
        self.rt.notify()
        return result

    def select(self, db, session_id, *, explicit=None, inherited=None, key):
        require(explicit is None or isinstance(explicit, str) and 0 < len(explicit) <= 256,
                "INVALID_REQUEST", "Choose a valid budget ID")
        require(not explicit or not inherited or explicit == inherited,
                "BUDGET_SCOPE_MISMATCH", "Descendants inherit their execution budget")
        identity = inherited or explicit
        if identity:
            row = one(db, "SELECT session_id FROM execution_budgets WHERE id=?", (identity,))
            require(row and row.session_id == session_id, "BUDGET_SCOPE_MISMATCH", "Budget belongs to another session or does not exist")
            return identity
        return self.create_in(db, session_id, BudgetPolicy(), key)

    def workflow(self, db, run_id, session_id, explicit=None, source_run_id=None):
        old = one(db, "SELECT budget_id FROM workflow_budgets WHERE run_id=?", (run_id,))
        if old:
            return old.budget_id
        if source_run_id is None:
            repair = one(db, "SELECT source_run_id FROM workflow_repairs WHERE run_id=?", (run_id,))
            source_run_id = repair.source_run_id if repair else None
        inherited = self.workflow(db, source_run_id, session_id) if source_run_id else None
        identity = self.select(db, session_id, explicit=explicit, inherited=inherited, key="workflow:" + run_id)
        db.execute("INSERT INTO workflow_budgets VALUES(?,?)", (run_id, identity))
        return identity

    def task(self, db, task_id, *, explicit=None, parent_task_id=None, workflow_run_id=None):
        old = one(db, "SELECT budget_id FROM task_budgets WHERE task_id=?", (task_id,))
        if old:
            return old.budget_id
        task = one(db, "SELECT session_id,retry_of_task_id FROM tasks WHERE id=?", (task_id,))
        require(task, "NOT_FOUND", "Task does not exist")
        parent_task_id = parent_task_id or task.retry_of_task_id
        member = None
        if not parent_task_id:
            member = one(db, "SELECT b.parent_task_id,b.id FROM instance_members m JOIN instance_batches b ON b.id=m.batch_id WHERE m.task_id=?", (task_id,))
            parent_task_id = member.parent_task_id if member else None
        if not workflow_run_id:
            owned = one(db, "SELECT run_id FROM workflow_tasks WHERE task_id=?", (task_id,))
            workflow_run_id = owned.run_id if owned else None
        inherited = (self.task(db, parent_task_id) if parent_task_id else
                     self.workflow(db, workflow_run_id, task.session_id) if workflow_run_id else None)
        if member:
            batch = one(db, "SELECT budget_id FROM batch_budgets WHERE batch_id=?", (member.id,))
            inherited = batch.budget_id if batch else self.select(db, task.session_id, inherited=inherited, key="batch:" + member.id)
            if not batch:
                db.execute("INSERT INTO batch_budgets VALUES(?,?)", (member.id, inherited))
        identity = self.select(db, task.session_id, explicit=explicit, inherited=inherited, key="task:" + task_id)
        db.execute("INSERT INTO task_budgets(task_id,budget_id) VALUES(?,?)", (task_id, identity))
        return identity

    def start(self, db, task_id):
        identity = self.task(db, task_id)
        db.execute("UPDATE execution_budgets SET started_at=COALESCE(started_at,?) WHERE id=?", (now_ms(), identity))
        return self.check(db, task_id)

    def check(self, db, task_id):
        row = one(db, "SELECT b.*,t.stalled_calls FROM execution_budgets b JOIN task_budgets t ON t.budget_id=b.id WHERE t.task_id=?", (task_id,))
        policy = json.loads(row.policy_json)
        require(row.started_at is None or now_ms() < row.started_at + policy["max_elapsed_seconds"] * 1000,
                "BUDGET_EXHAUSTED", "Cumulative execution time limit reached", details={"stage": "budget", "budget_id": row.id, "limit": "max_elapsed_seconds"})
        return row, policy

    def reserve_model(self, db, request, identity, purpose, input_tokens, output_tokens):
        row, policy = self.check(db, request.task_id)
        require(row.stalled_calls < policy["max_stalled_model_calls"], "PROGRESS_STALLED",
                "Model loop has no new verified evidence; partial result is retained",
                details={"stage": "budget", "budget_id": row.id, "limit": "max_stalled_model_calls"})
        output_tokens = min(output_tokens or 4096, policy["max_output_tokens"] - row.output_tokens)
        for name, value in (("max_model_calls", row.model_calls + 1), ("max_input_tokens", row.input_tokens + input_tokens),
                            ("max_output_tokens", row.output_tokens + max(1, output_tokens))):
            require(value <= policy[name], "BUDGET_EXHAUSTED", "Cumulative model budget is exhausted",
                    details={"stage": "budget", "budget_id": row.id, "limit": name})
        db.execute("INSERT INTO budget_entries(id,budget_id,task_id,run_id,kind,purpose,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,'model',?,?,?,?)",
                   (identity, row.id, request.task_id, request.run_id, purpose, input_tokens, output_tokens, now_ms()))
        db.execute("UPDATE execution_budgets SET model_calls=model_calls+1,input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?",
                   (input_tokens, output_tokens, row.id))
        db.execute("UPDATE task_budgets SET stalled_calls=stalled_calls+1 WHERE task_id=?", (request.task_id,))
        return output_tokens

    def settle_model(self, db, identity, usage):
        row = one(db, "SELECT * FROM budget_entries WHERE id=? AND state='RESERVED'", (identity,))
        if row is None:
            return
        usage = usage or {}
        incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
        outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
        known = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
        if known:
            db.execute("UPDATE execution_budgets SET input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?",
                       (incoming - row.input_tokens, outgoing - row.output_tokens, row.budget_id))
        db.execute("UPDATE budget_entries SET state='SETTLED',input_tokens=?,output_tokens=?,estimated=? WHERE id=?",
                   (incoming if known else row.input_tokens, outgoing if known else row.output_tokens, int(not known), identity))

    def tool(self, db, request, identity, name):
        if one(db, "SELECT id FROM budget_entries WHERE id=?", (identity,)):
            return
        row, policy = self.check(db, request.task_id)
        require(row.tool_calls < policy["max_tool_calls"], "BUDGET_EXHAUSTED", "Cumulative tool budget is exhausted",
                details={"stage": "budget", "budget_id": row.id, "limit": "max_tool_calls"})
        db.execute("INSERT INTO budget_entries(id,budget_id,task_id,run_id,kind,purpose,estimated,state,created_at) VALUES(?,?,?,?,'tool',?,0,'SETTLED',?)",
                   (identity, row.id, request.task_id, request.run_id, name, now_ms()))
        db.execute("UPDATE execution_budgets SET tool_calls=tool_calls+1 WHERE id=?", (row.id,))

    def progress(self, db, task_id, source, value):
        row = one(db, "SELECT b.policy_json FROM task_budgets t JOIN execution_budgets b ON b.id=t.budget_id WHERE t.task_id=?", (task_id,))
        if not row:
            return
        builtin = {"read_context", "read_history", "read_file", "write_file", "edit_file", "publish_artifact", "put_file", "put_bytes"}
        if source not in builtin and source not in json.loads(row.policy_json)["progress_tools"]:
            return
        fingerprint = content_hash({"source": source, "value": value})
        db.execute("INSERT OR IGNORE INTO budget_progress VALUES(?,?,?,?)", (task_id, fingerprint, source, now_ms()))
        if db.changes():
            db.execute("UPDATE task_budgets SET stalled_calls=0 WHERE task_id=?", (task_id,))

    async def get(self, budget_id):
        def read(db):
            row = one(db, "SELECT * FROM execution_budgets WHERE id=?", (budget_id,))
            require(row, "NOT_FOUND", "Budget does not exist")
            policy = json.loads(row.policy_json)
            counts = {name: row[name] for name in ("model_calls", "input_tokens", "output_tokens", "tool_calls")}
            estimates = one(db, "SELECT COUNT(*) AS count FROM budget_entries WHERE budget_id=? AND kind='model' AND estimated=1", (budget_id,)).count
            return {"id": row.id, "session_id": row.session_id, "policy": policy, "policy_hash": content_hash(policy), "usage": counts,
                    "remaining": {key: max(0, policy["max_" + key] - value) for key, value in counts.items()},
                    "started_at": row.started_at, "deadline_at": row.started_at + policy["max_elapsed_seconds"] * 1000 if row.started_at else None,
                    "estimated_model_calls": estimates}
        return await self.rt.store.call(read)

    async def for_task(self, task_id):
        row = await self.rt.store.call(lambda db: one(db, "SELECT budget_id,stalled_calls FROM task_budgets WHERE task_id=?", (task_id,)))
        require(row, "NOT_FOUND", "No budget is assigned to this task")
        return {**await self.get(row.budget_id), "task_stalled_calls": row.stalled_calls}

    async def entries(self, budget_id, cursor=None, limit=50):
        await self.get(budget_id)
        limit = max(1, min(int(limit), 100))
        values = await self.rt.store.call(lambda db: rows(db, "SELECT rowid AS position,* FROM budget_entries WHERE budget_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                                                     (budget_id, int(cursor or 0), limit + 1)))
        return {"items": values[:limit], "next_cursor": str(values[limit - 1].position) if len(values) > limit else None}
