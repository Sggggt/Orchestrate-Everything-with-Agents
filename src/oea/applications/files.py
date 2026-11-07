"""Application file access keeps shared immutable artifacts and private input copies."""

import hashlib
import os
from pathlib import Path
import shutil

from oea.artifacts.service import file_io
from oea.catalog.service import need
from oea.contracts.errors import require
from oea.storage.persistence import new_id, one
from oea.workspace.paths import safe_child


class ApplicationFiles:
    def __init__(self, business):
        self.business, self.host = business, business.host

    @staticmethod
    def consumer(app_id, conversation_id):
        return "oap:" + app_id + ":" + conversation_id

    async def begin(self, app_id, request, key):
        from oea.applications.business import require_conversation
        def check(db):
            conversation = require_conversation(db, app_id, request.conversation_id)
            return conversation, need(db, "sessions", conversation["session_id"])["project_id"]
        conversation, project = await self.host.store.call(check)
        return await self.host.artifacts.begin_upload(project, request.name, request.bytes, request.sha256, key,
            session_id=conversation["session_id"], consumer_id=self.consumer(app_id, request.conversation_id))

    async def owner(self, app_id, upload_id):
        def read(db):
            row = one(db, "SELECT u.*,c.id AS conversation_id,c.app_id FROM artifact_uploads u "
                      "JOIN application_conversations c ON c.session_id=u.session_id WHERE u.id=? AND c.app_id=?", (upload_id, app_id))
            require(row is not None, "PERMISSION_DENIED", "Upload is not available to this application")
            return row
        return await self.host.store.call(read)

    async def get(self, app_id, upload_id):
        row = await self.owner(app_id, upload_id)
        return await self.host.artifacts.get_upload(upload_id, row["project_id"], consumer_id=self.consumer(app_id, row["conversation_id"]))

    async def write(self, app_id, upload_id, chunks):
        row = await self.owner(app_id, upload_id)
        return await self.host.artifacts.write_upload(upload_id, row["project_id"], chunks,
            consumer_id=self.consumer(app_id, row["conversation_id"]))

    async def commit(self, app_id, upload_id):
        row = await self.owner(app_id, upload_id)
        return await self.host.artifacts.commit_upload(upload_id, row["project_id"],
            consumer_id=self.consumer(app_id, row["conversation_id"]))

    async def materialize(self, app_id, conversation_id, artifact_ids):
        if not artifact_ids:
            return
        from oea.applications.business import require_conversation, require_artifact
        def read(db):
            conversation = require_conversation(db, app_id, conversation_id)
            records = [require_artifact(db, app_id, identity) for identity in artifact_ids]
            require(all(row["conversation_id"] == conversation_id for row in records), "PERMISSION_DENIED", "Inputs must belong to this conversation")
            return conversation, records
        conversation, records = await self.host.store.call(read)
        directory = self.host.root / "application-workspaces" / app_id / conversation["binding_id"] / conversation_id / "inputs"
        def copy(record):
            original = safe_child(self.host.root / "artifacts", record["id"])
            require(original == Path(record["path"]).resolve(), "ARTIFACT_INVALID", "Artifact is outside its immutable store")
            target = safe_child(directory, record["id"] + "/" + record["name"])
            if target.exists():
                with target.open("rb") as existing:
                    require(hashlib.file_digest(existing, "sha256").hexdigest() == record["sha256"], "ARTIFACT_CHANGED", "Assigned input changed")
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = safe_child(target.parent, new_id("input-part"))
            try:
                with original.open("rb") as source, temporary.open("xb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                require(temporary.stat().st_size == record["bytes"], "ARTIFACT_CHANGED", "Assigned input size changed")
                with temporary.open("rb") as source:
                    require(hashlib.file_digest(source, "sha256").hexdigest() == record["sha256"], "ARTIFACT_CHANGED", "Assigned input hash changed")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        for record in records:
            await file_io(copy, record)

    async def download(self, app_id, artifact_id):
        from oea.applications.business import require_artifact
        record = await self.host.store.call(lambda db: require_artifact(db, app_id, artifact_id))
        path = safe_child(self.host.root / "artifacts", record["id"])
        require(path == Path(record["path"]).resolve(), "ARTIFACT_INVALID", "Artifact is outside its immutable store")
        return record, path
