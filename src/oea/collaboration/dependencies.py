"""Event-driven wake-ups for a native Agent graph's explicit child wait."""

import asyncio
import json

from oea.catalog.service import need
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import rows


class RunDependencies:
    def __init__(self, host):
        self.host, self.jobs = host, {}

    def watch(self, run_id, wait):
        if self.host.closing or run_id in self.jobs:
            return
        task = asyncio.create_task(self._observe(run_id, wait))
        self.jobs[run_id] = task
        def finished(completed):
            if self.jobs.get(run_id) is completed:
                self.jobs.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)

    async def _child(self, parent, child_id):
        while True:
            changed = self.host.run_signal(child_id)
            child = await self.host.store.call(lambda db: need(db, "runs", child_id))
            require((child["root_run_id"], child["root_workflow_run_id"]) == (parent["root_run_id"], parent["root_workflow_run_id"]),
                    "PERMISSION_DENIED", "Wait belongs to another intent")
            if child["state"] in {"completed", "failed", "cancelled", "needs_review"}:
                self.host.notify_run(child_id)
                return {"run_id": child_id, "state": child["state"]}
            await changed.wait()

    async def _observe(self, run_id, wait):
        parent = await self.host.store.call(lambda db: need(db, "runs", run_id))
        require(wait.get("run_ids") and run_id not in wait["run_ids"], "INVALID_REQUEST", "Choose related child work")
        children = asyncio.gather(*(self._child(parent, identity) for identity in wait["run_ids"]))
        try:
            while True:
                changed = self.host.run_signal(run_id)
                current = await self.host.runs.get(run_id)
                if current["state"] != "waiting" or not current["wait"] or current["wait"].get("interrupt_id") != wait["interrupt_id"]:
                    return
                if children.done():
                    values = children.result()
                    if any(value["state"] == "needs_review" for value in values):
                        await self.host.finish(run_id, "needs_review", wait=wait, error=RuntimeFault("OUTCOME_UNCERTAIN", "Inspect the original child's unresolved actions before explicitly resuming"))
                    elif wait.get("policy", "all_settled") == "all_succeeded" and any(value["state"] != "completed" for value in values):
                        await self.host.finish(run_id, "failed", wait=wait, error=RuntimeFault("CHILD_FAILED", "Required child work did not complete", details={"children": values}))
                    else:
                        # This resumes an explicit normal dependency wait.
                        # needs_review and confirmation are never auto-resumed.
                        await self.host.runs.resume(run_id, "dependencies:" + wait["interrupt_id"], input={"children": values}, refresh_model=False, refresh_capabilities=False)
                    return
                observer = asyncio.create_task(changed.wait())
                try:
                    await asyncio.wait([observer, children], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)
        except Exception as error:
            if not self.host.closing:
                current = await self.host.runs.get(run_id)
                if current["state"] == "waiting" and current["wait"] and current["wait"].get("interrupt_id") == wait["interrupt_id"]:
                    fault = error if isinstance(error, RuntimeFault) else RuntimeFault("DEPENDENCY_OBSERVATION_FAILED", "Child receipt observation failed", details={"cause": type(error).__name__})
                    await self.host.finish(run_id, "failed", wait=wait, error=fault)
        finally:
            children.cancel()
            await asyncio.gather(children, return_exceptions=True)

    async def open(self):
        pending = await self.host.store.call(lambda db: rows(db, "SELECT id,wait_json FROM runs WHERE state='waiting' AND json_extract(wait_json,'$.kind')='runs'"))
        for run in pending:
            self.watch(run["id"], json.loads(run["wait_json"]))

    async def aclose(self):
        jobs = tuple(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
