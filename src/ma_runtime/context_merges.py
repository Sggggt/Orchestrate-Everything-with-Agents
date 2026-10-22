"""Explicit evidence merges; execution checkpoints always retain one main parent."""

import json

from .errors import require
from .models import ContextMergeSpec, content_hash, json_dump, new_id, now_ms
from .store import emit, one, rows


def checkpoint_contains_run(db, thread_id, checkpoint_id, run_id):
    return bool(one(db, "WITH RECURSIVE lineage(thread_id,checkpoint_id) AS (SELECT ?,? UNION ALL "
        "SELECT b.parent_thread_id,b.checkpoint_id FROM thread_branches b JOIN lineage l ON b.thread_id=l.thread_id) "
        "SELECT 1 FROM lineage l JOIN checkpoints p ON p.id=l.checkpoint_id JOIN runs cutoff ON cutoff.id=p.created_by_run_id "
        "JOIN runs r ON r.rowid<=cutoff.rowid JOIN tasks t ON t.id=r.task_id AND t.thread_id=l.thread_id "
        "WHERE r.id=? LIMIT 1", (thread_id, checkpoint_id, run_id)))


def checkpoint_contains_context(db, thread_id, checkpoint_id, reference):
    return bool(one(db, "WITH RECURSIVE lineage(thread_id,checkpoint_id) AS (SELECT ?,? UNION ALL "
        "SELECT b.parent_thread_id,b.checkpoint_id FROM thread_branches b JOIN lineage l ON b.thread_id=l.thread_id) "
        "SELECT 1 FROM lineage l JOIN checkpoints p ON p.id=l.checkpoint_id JOIN runs cutoff ON cutoff.id=p.created_by_run_id "
        "JOIN tasks t ON t.thread_id=l.thread_id JOIN runs r ON r.task_id=t.id AND r.rowid<=cutoff.rowid "
        "JOIN context_access a ON a.task_id=t.id AND a.reference=? "
        "WHERE a.first_run_id IS NULL OR (SELECT rowid FROM runs WHERE id=a.first_run_id)<=cutoff.rowid LIMIT 1",
        (thread_id, checkpoint_id, reference)))


class ContextMerges:
    def __init__(self, runtime):
        self.rt = runtime

    def head(self, db, thread_id):
        return one(db, "SELECT m.* FROM thread_context_heads h JOIN context_merges m ON m.id=h.merge_id WHERE h.thread_id=?", (thread_id,))

    @staticmethod
    def view(row):
        return {"id": row.id, "thread_id": row.thread_id, "parent_id": row.parent_id,
                "target_revision": row.target_revision, "target_checkpoint_id": row.target_checkpoint_id,
                "sources": json.loads(row.sources_json), "evidence": json.loads(row.evidence_json),
                "resolutions": json.loads(row.resolutions_json)}

    def validate_evidence(self, db, evidence, project_id, agent_id):
        artifact = one(db, "SELECT a.*,s.project_id FROM artifacts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?", (evidence["reference"],))
        require(artifact and artifact.project_id == project_id, "PROJECT_MISMATCH", "Selected evidence belongs to another project")
        require(artifact.state == "READY" and artifact.sha256 == evidence["sha256"], "SOURCE_VERSION_CONFLICT", "Selected evidence no longer matches its frozen version")
        if evidence["kind"] == "context":
            source = one(db, "SELECT * FROM context_sources WHERE reference=?", (artifact.id,))
            require(source and source.source_hash == artifact.sha256, "SOURCE_VERSION_CONFLICT", "Context source is unavailable")
            self.rt.contexts.check_origin_for_agent(agent_id, source.origin)
        return artifact

    async def merge(self, spec):
        self.rt.admit()
        spec = ContextMergeSpec.model_validate(spec)
        digest = content_hash(spec)
        def commit(db):
            from .services import need, resolve_project
            prior = one(db, "SELECT * FROM context_merges WHERE thread_id=? AND idempotency_key=?", (spec.thread_id, spec.idempotency_key))
            if prior:
                require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Merge key has different input")
                return {**self.view(prior), "sha256": prior.manifest_hash, "reused": True}
            target = need(db, "threads", spec.thread_id)
            session = need(db, "sessions", target.session_id)
            resolve_project(db, session.project_id)
            require(session.state == "OPEN", "SESSION_CLOSED", "Restore an open session before merging")
            require(target.revision == spec.expected_revision, "REVISION_CONFLICT", "Target thread has changed")
            require(not target.active_run_id and not target.owner_task_id and not target.blocked_reason
                    and not one(db, "SELECT 1 FROM tasks WHERE thread_id=? AND state IN ('QUEUED','UNCERTAIN') LIMIT 1", (target.id,)),
                    "THREAD_BUSY", "Merge only into a settled target thread")
            candidates, source_ids = {}, set()
            for selected in spec.sources:
                require(selected.thread_id != target.id and selected.thread_id not in source_ids, "INVALID_REQUEST", "Select each source thread once, excluding the target")
                source_ids.add(selected.thread_id)
                source = need(db, "threads", selected.thread_id)
                source_session = need(db, "sessions", source.session_id)
                require(source_session.project_id == session.project_id, "PROJECT_MISMATCH", "Merge sources must share a project")
                require(source.revision == selected.revision and source.latest_checkpoint_id == selected.checkpoint_id,
                        "SOURCE_VERSION_CONFLICT", "Source head changed; select its new revision explicitly")
                require(not source.active_run_id and not source.blocked_reason,
                        "SOURCE_UNSETTLED", "A source with active or unknown execution cannot be reused")
                checkpoint = need(db, "checkpoints", selected.checkpoint_id)
                require(checkpoint.thread_id == source.id, "INVALID_CHECKPOINT", "Source checkpoint belongs to another thread")
                for item in selected.evidence:
                    value = item.model_dump(mode="json", exclude={"schema_version"})
                    artifact = self.validate_evidence(db, value, session.project_id, target.agent_id)
                    if item.kind == "context":
                        inherited = one(db, "SELECT m.evidence_json FROM checkpoint_context_heads h JOIN context_merges m ON m.id=h.merge_id WHERE h.checkpoint_id=?", (checkpoint.id,))
                        from_parent = inherited and any(entry["reference"] == item.reference for entry in json.loads(inherited.evidence_json))
                        assigned = checkpoint_contains_context(db, source.id, checkpoint.id, item.reference)
                        require(from_parent or assigned, "PERMISSION_DENIED", "Evidence was not available at the selected source checkpoint")
                    else:
                        provenance = json.loads(artifact.metadata_json).get("provenance", {})
                        if provenance.get("run_id"):
                            require(checkpoint_contains_run(db, source.id, checkpoint.id, provenance["run_id"]),
                                    "PERMISSION_DENIED", "Artifact was not produced on the selected source path")
                    value["source_thread_id"] = source.id
                    candidates.setdefault(item.name, []).append(value)
            parent = self.head(db, target.id)
            effective = {item["name"]: item for item in json.loads(parent.evidence_json)} if parent else {}
            conflicts = set()
            for name, values in candidates.items():
                variants = {(item["kind"], item["sha256"]) for item in values}
                if name in effective:
                    variants.add((effective[name]["kind"], effective[name]["sha256"]))
                    values = [*values, effective[name]]
                if len(variants) > 1:
                    conflicts.add(name)
                    resolution = spec.resolutions.get(name)
                    chosen = next((item for item in values if resolution and item["reference"] == resolution.reference), None)
                    require(chosen is not None, "CONTEXT_MERGE_CONFLICT", "Different evidence occupies the same name; provide an explicit reference and reason", details={"name": name, "references": [item["reference"] for item in values]})
                else:
                    chosen = values[0]
                effective[name] = chosen
            require(set(spec.resolutions) == conflicts, "INVALID_REQUEST", "Resolutions must identify the conflicting evidence names")
            require(len(effective) <= 64, "CONTEXT_LIMIT", "A merged context contains at most 64 named evidence references")
            for value in effective.values():
                self.validate_evidence(db, value, session.project_id, target.agent_id)
            identity = new_id("merge")
            view = {"id": identity, "thread_id": target.id, "parent_id": parent.id if parent else None,
                    "target_revision": target.revision, "target_checkpoint_id": target.latest_checkpoint_id,
                    "sources": [source.model_dump(mode="json", exclude={"schema_version"}) for source in spec.sources],
                    "evidence": list(effective.values()),
                    "resolutions": {name: value.model_dump(mode="json", exclude={"schema_version"}) for name, value in spec.resolutions.items()}}
            sha256 = content_hash(view)
            db.execute("INSERT INTO context_merges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identity, target.id, view["parent_id"], target.revision,
                target.latest_checkpoint_id, json_dump(view["sources"]), json_dump(view["evidence"]), json_dump(view["resolutions"]), sha256, spec.idempotency_key, digest, now_ms()))
            db.execute("INSERT INTO thread_context_heads VALUES(?,?) ON CONFLICT(thread_id) DO UPDATE SET merge_id=excluded.merge_id", (target.id, identity))
            db.execute("UPDATE threads SET revision=revision+1 WHERE id=?", (target.id,))
            emit(db, "context.merged", session_id=target.session_id, thread_id=target.id,
                 payload={"merge_id": identity, "revision": target.revision + 1, "sources": list(source_ids)})
            return {**view, "sha256": sha256, "reused": False}
        result = await self.rt.store.call(commit, transaction=True)
        self.rt.notify()
        return result

    async def get(self, merge_id):
        from .services import need
        row = await self.rt.store.call(lambda db: need(db, "context_merges", merge_id))
        return {**self.view(row), "sha256": row.manifest_hash}

    def assign(self, db, task, thread, spec, project_id):
        head = self.head(db, thread.id)
        if head is None:
            return
        require(len(spec.context_refs) < 64, "CONTEXT_LIMIT", "Reserve one context reference for the merged evidence manifest")
        db.execute("INSERT INTO task_context_heads VALUES(?,?)", (task, head.id))
        for evidence in json.loads(head.evidence_json):
            self.validate_evidence(db, evidence, project_id, thread.agent_id)
            if evidence["kind"] == "context":
                db.execute("INSERT OR IGNORE INTO context_access(task_id,reference) VALUES(?,?)", (task, evidence["reference"]))
            else:
                db.execute("INSERT OR IGNORE INTO task_artifact_access VALUES(?,?)", (task, evidence["reference"]))

    def references(self, db, task_id):
        head = one(db, "SELECT merge_id FROM task_context_heads WHERE task_id=?", (task_id,))
        return ("merge:" + head.merge_id,) if head else ()

    def checkpoint(self, db, thread_id, checkpoint_id):
        db.execute("INSERT INTO checkpoint_context_heads SELECT ?,merge_id FROM thread_context_heads WHERE thread_id=?", (checkpoint_id, thread_id))

    def fork(self, db, checkpoint_id, thread_id, new_checkpoint_id):
        head = one(db, "SELECT merge_id FROM checkpoint_context_heads WHERE checkpoint_id=?", (checkpoint_id,))
        if head:
            db.execute("INSERT INTO thread_context_heads VALUES(?,?)", (thread_id, head.merge_id))
            db.execute("INSERT INTO checkpoint_context_heads VALUES(?,?)", (new_checkpoint_id, head.merge_id))
