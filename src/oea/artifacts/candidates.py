"""Unpublished file observations retain provenance without implying acceptance."""

import hashlib
import json
from pathlib import Path

from oea.catalog.service import need
from oea.contracts.configuration import AgentDefinition, ProjectWorkspace, manager_snapshot
from oea.contracts.errors import require
from oea.storage.persistence import encode, now_ms, one, rows
from oea.workspace.environment import ExecutionEnvironment, WorkspacePaths, intersect_workspace


class CandidateFiles:
    def __init__(self, host):
        self.host = host

    def scope(self, db, run_id, project_id):
        run = one(db, "SELECT r.*,i.project_id FROM runs r JOIN instances i ON i.id=r.instance_id WHERE r.id=?", (run_id,))
        require(run is not None, "NOT_FOUND", "Run does not exist")
        require(run["project_id"] == project_id, "PERMISSION_DENIED", "Run belongs to another project")
        return run

    async def environment(self, run_id, project_id):
        def read(db):
            run = self.scope(db, run_id, project_id)
            return run, need(db, "instances", run["instance_id"])
        run, instance = await self.host.store.call(read)
        saved = AgentDefinition.model_validate_json(run["config_json"])
        current = self.host.definitions.get(instance["agent_id"])
        require(current is not None and not current.removed, "PERMISSION_DENIED", "The Agent's live file grants are unavailable")
        if instance["role"] == "manager":
            require(current.manager is not None, "PERMISSION_DENIED", "Manager file grants were removed")
            current = manager_snapshot(current)
        project = await self.host.projects.get(project_id)
        settings = current.workspace.resolved(ProjectWorkspace.model_validate(project["workspace"]), project["folder"])
        paths = WorkspacePaths.for_instance(saved.workspace, instance["id"], run_id, self.host.root,
                                           binding=json.loads(instance["workspace_json"]))
        paths, settings = intersect_workspace(paths, saved.workspace, settings)
        return ExecutionEnvironment(paths, settings)

    async def retain(self, run_id, project_id, observations):
        if not observations:
            return []
        def write(db):
            run = self.scope(db, run_id, project_id)
            published = self.publications(db, run_id, {value["sha256"] for value in observations if value["sha256"]})
            result = []
            for value in observations:
                body = encode(value)
                digest = hashlib.sha256(body.encode()).hexdigest()
                identity = "candidate_" + hashlib.sha256((run_id + "\0" + digest).encode()).hexdigest()[:32]
                origin = "file_candidate:" + run_id
                db.execute("INSERT OR IGNORE INTO context_sources VALUES(?,?,?,?,?,?,?,?,?)",
                    (identity, project_id, run["instance_id"], run_id, origin, digest, body, None, now_ms()))
                published_id = published.get(value["sha256"])
                result.append({"reference": identity, **value, "published": bool(published_id) if value["sha256"] else None,
                               "published_artifact_id": published_id, "source_state": "not_checked"})
            return result
        return await self.host.store.call(write, transaction=True)

    @staticmethod
    def publications(db, run_id, fingerprints):
        if not fingerprints:
            return {}
        found = rows(db, "SELECT MIN(id) AS id,sha256 FROM artifacts WHERE run_id=? AND sha256 IN (" +
                     ",".join("?" for _ in fingerprints) + ") GROUP BY sha256", (run_id, *fingerprints))
        return {row["sha256"]: row["id"] for row in found}

    async def patches(self, run_id, project_id, cursor, limit):
        def read(db):
            self.scope(db, run_id, project_id)
            return rows(db, """SELECT rowid AS position,id,json_extract(result_json,'$.path') AS path,
                json_extract(result_json,'$.sha256') AS sha256,json_extract(result_json,'$.bytes') AS bytes FROM actions
                WHERE run_id=? AND name='apply_patch' AND state='completed'
                AND COALESCE(json_extract(result_json,'$.isError'),0)=0
                AND COALESCE(json_extract(result_json,'$.deleted'),0)=0
                AND json_extract(result_json,'$.sha256') IS NOT NULL
                AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?""", (run_id, cursor, cursor, limit + 1))
        found = await self.host.store.call(read)
        page = found[:limit]
        values = []
        for row in page:
            values.append({"path": row["path"], "name": Path(row["path"]).name,
                           "sha256": row["sha256"], "bytes": row["bytes"],
                           "source": "patch_receipt", "action_id": row["id"]})
        return {"items": await self.retain(run_id, project_id, values),
                "cursor": page[-1]["position"] if len(found) > limit else None, "source": "patches"}

    async def outputs(self, run_id, project_id, directory, cursor, limit, version):
        environment = await self.environment(run_id, project_id)
        result = await environment.observe_files({"operation": "list_outputs", "directory": directory,
                                                  "cursor": cursor, "limit": limit, "version": version})
        observations = [{**value, "source": "output_directory", "sha256": None}
                        for value in result.pop("files")]
        return {**result, "items": await self.retain(run_id, project_id, observations), "source": "outputs"}

    async def check(self, reference, project_id, check_source):
        def read(db):
            source = need(db, "context_sources", reference)
            require(source["project_id"] == project_id and source["origin"] == "file_candidate:" + source["run_id"],
                    "PERMISSION_DENIED", "Reference is not a file candidate in this project")
            saved = json.loads(source["body"])
            return source, saved, self.publications(db, source["run_id"], {saved["sha256"]} if saved["sha256"] else set())
        source, saved, published = await self.host.store.call(read)
        published_id = published.get(saved["sha256"])
        result = {"reference": reference, **saved, "published": bool(published_id) if saved["sha256"] else None,
                  "published_artifact_id": published_id, "source_state": "not_checked"}
        if not check_source:
            return result
        environment = await self.environment(source["run_id"], project_id)
        current = await environment.observe_files({"operation": "inspect_file", "path": saved["path"]})
        if not current["exists"]:
            return {**result, "source_state": "missing", "current": current}
        same = current["sha256"] == saved["sha256"] if saved["sha256"] else (
            current["bytes"] == saved["bytes"] and current["modified_ns"] == saved["modified_ns"])
        state = "unchanged" if same and saved["sha256"] else "first_verification" if same else "changed"
        if saved["sha256"] == current["sha256"]:
            verified = result
        else:
            observed = {**current, "name": saved["name"], "source": "verification"}
            verified = (await self.retain(source["run_id"], project_id, [observed]))[0]
        current.update(published=verified["published"], published_artifact_id=verified["published_artifact_id"])
        return {**result, "source_state": state, "current": current, "current_reference": verified["reference"],
                "review_required": state == "changed"}
