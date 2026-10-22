"""Immutable workflow origin and explicit execution ownership within transactions."""

from .errors import require
from .store import one


def bind_workflow_thread(db, thread_id, run_id):
    prior = one(db, "SELECT run_id FROM thread_workflow_runs WHERE thread_id=?", (thread_id,))
    if prior:
        require(prior.run_id == run_id, "OWNERSHIP_CONFLICT", "Thread already belongs to another workflow run")
        return
    db.execute("INSERT INTO thread_workflow_runs VALUES(?,?)", (thread_id, run_id))


def own_workflow_task(db, task_id, run_id):
    db.execute("INSERT INTO workflow_tasks VALUES(?,?)", (task_id, run_id))


def workflow_owner(db, task_id):
    owner = one(db, "SELECT run_id FROM workflow_tasks WHERE task_id=?", (task_id,))
    return owner.run_id if owner else None
