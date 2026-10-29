"""Shared Skill sources, immutable bodies, and explicit current grants."""

import asyncio
import hashlib
from pathlib import Path

import yaml

from pydantic import Field

from oea.contracts.configuration import Configuration
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, now_ms, one, rows


class SkillSource(Configuration):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    path: Path
    project_id: str | None = None
    name: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=640)
    enabled: bool = True


class Skills:
    def __init__(self, host):
        self.host, self.definitions = host, {}

    async def open(self):
        values = await self.host.store.call(lambda db: rows(db, "SELECT config_json,revision FROM skills WHERE removed=0"))
        self.definitions = {definition.id: (definition, value["revision"]) for value in values for definition in [SkillSource.model_validate_json(value["config_json"])]}

    def source_path(self, directory, relative):
        root = directory.expanduser().resolve()
        require(not root.is_relative_to(self.host.root) and not self.host.root.is_relative_to(root), "PERMISSION_DENIED", "A Skill source cannot expose host state")
        target = (root / relative).resolve()
        require(target.is_relative_to(root) and target.is_file(), "PERMISSION_DENIED", "Skill file is unavailable or outside its source directory")
        return target

    def main_body(self, definition):
        path = self.source_path(definition.path, "SKILL.md")
        require(path.stat().st_size <= 65536, "SKILL_TOO_LARGE", "Skill instructions exceed 64 KiB; split supporting references")
        body = path.read_text(encoding="utf-8-sig")
        return body, hashlib.sha256(body.encode("utf-8")).hexdigest()

    async def schema(self):
        return SkillSource.model_json_schema()

    @staticmethod
    def metadata(body, *, required=False):
        lines = body.splitlines()
        if not lines or lines[0] != "---":
            require(not required, "INVALID_SKILL", "Directory registration requires SKILL.md frontmatter")
            return {}
        try:
            end = lines.index("---", 1)
            value = yaml.safe_load("\n".join(lines[1:end]))
        except (ValueError, yaml.YAMLError):
            raise RuntimeFault("INVALID_SKILL", "SKILL.md frontmatter is invalid") from None
        require(isinstance(value, dict), "INVALID_SKILL", "Skill frontmatter must be a mapping")
        fields = ("name", "description", "license", "compatibility", "allowed-tools")
        require(all(key not in value or isinstance(value[key], str) for key in fields), "INVALID_SKILL", "Skill metadata fields must be text")
        if required:
            require(bool(value.get("name")) and bool(value.get("description", "").strip()), "INVALID_SKILL", "Skill frontmatter needs a name and description")
        return {key: value[key] for key in fields if key in value}

    async def register_directory(self, directory: Path, project_id: str, *, enabled: bool = True):
        """Register an explicit standard Skill directory without copying references."""
        self.host.assert_open()
        directory = Path(directory).expanduser()
        require(directory.is_absolute() and directory.is_dir(), "INVALID_REQUEST", "Choose an existing absolute Skill directory")
        source = SkillSource(id="source", path=directory.resolve(), project_id=project_id, enabled=enabled)
        body, sha = await asyncio.to_thread(self.main_body, source)
        metadata = self.metadata(body, required=True)
        definition = SkillSource(id=metadata["name"], name=metadata["name"], description=metadata["description"], path=source.path, project_id=project_id, enabled=enabled)
        return await self._register(definition, body, sha, expected_revision=0)

    async def describe(self, skill_id: str, project_id: str):
        saved = await self.get(skill_id, project_id)
        version = await self.body(skill_id, saved["revision"])
        bound = [definition.id for definition in self.host.definitions.values() if definition.project_id in {None, project_id} and any(binding.id == skill_id for binding in definition.skills)]
        return {**saved, "metadata": self.metadata(version["body"]), "bound_agents": bound}

    async def register(self, definition: SkillSource, *, expected_revision: int | None = None):
        """Read one Skill body and register its source without copying a directory."""
        self.host.assert_open()
        definition = SkillSource.model_validate(definition)
        definition = definition.model_copy(update={"path": definition.path.expanduser().resolve()})
        body, sha = await asyncio.to_thread(self.main_body, definition)
        metadata = self.metadata(body)
        definition = SkillSource.model_validate(definition.model_dump() | {"name": definition.name or metadata.get("name") or definition.id,
            "description": definition.description or metadata.get("description", "")})
        return await self._register(definition, body, sha, expected_revision=expected_revision)

    async def _register(self, definition, body, sha, *, expected_revision):
        def save(db):
            if definition.project_id:
                require(one(db, "SELECT 1 FROM projects WHERE id=?", (definition.project_id,)), "NOT_FOUND", "Project does not exist")
            previous = one(db, "SELECT * FROM skills WHERE id=?", (definition.id,))
            require(not previous or previous["project_id"] == definition.project_id, "PROJECT_MISMATCH", "Skill cannot change its project")
            require(expected_revision is None or expected_revision == (previous["revision"] if previous else 0), "REVISION_CONFLICT", "Skill configuration changed")
            revision = previous["revision"] + 1 if previous else 1
            db.execute("INSERT INTO skills VALUES(?,?,?,?,0,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,revision=excluded.revision,removed=0,updated_at=excluded.updated_at",
                (definition.id, definition.project_id, definition.model_dump_json(), revision, now_ms()))
            db.execute("INSERT OR IGNORE INTO skill_bodies VALUES(?,?)", (sha, body))
            db.execute("INSERT INTO skill_versions VALUES(?,?,?,?,?,?,?)", (definition.id, revision, str(definition.path), sha, definition.name, definition.description, now_ms()))
            event(db, "skill.saved", project_id=definition.project_id, data={"skill_id": definition.id, "revision": revision})
            return revision
        revision = await self.host.store.call(save, transaction=True)
        self.definitions[definition.id] = (definition, revision)
        self.host.notify()
        return {"definition": definition.model_dump(mode="json"), "revision": revision, "sha256": sha}

    @staticmethod
    def scoped(db, skill_id, project_id, *, include_removed=False):
        require(one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,)), "NOT_FOUND", "Project does not exist")
        record = one(db, "SELECT * FROM skills WHERE id=? AND (? OR removed=0) AND (project_id=? OR project_id IS NULL)", (skill_id, int(include_removed), project_id))
        require(record, "PERMISSION_DENIED", "Skill is unavailable or belongs to another project")
        return record

    async def get(self, skill_id: str, project_id: str):
        def read(db):
            record = self.scoped(db, skill_id, project_id)
            definition = SkillSource.model_validate_json(record["config_json"])
            version = one(db, "SELECT sha256 FROM skill_versions WHERE skill_id=? AND revision=?", (skill_id, record["revision"]))
            return {"definition": definition.model_dump(mode="json"), "revision": record["revision"], "sha256": version["sha256"]}
        return await self.host.store.call(read)

    async def list(self, project_id: str, *, cursor: int = 0, limit: int = 25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded page")
        values = await self.host.store.call(lambda db: rows(db, "SELECT rowid AS position,* FROM skills WHERE removed=0 AND (project_id=? OR project_id IS NULL) AND rowid>? ORDER BY rowid LIMIT ?", (project_id, cursor, limit + 1)))
        bindings = {}
        for definition in self.host.definitions.values():
            if definition.project_id in {None, project_id}:
                for binding in definition.skills:
                    bindings.setdefault(binding.id, []).append(definition.id)
        return {"items": [{"position": value["position"], "definition": SkillSource.model_validate_json(value["config_json"]).model_dump(mode="json"), "revision": value["revision"], "bound_agents": bindings.get(value["id"], [])} for value in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def refresh(self, skill_id: str, project_id: str, expected_revision: int):
        """Explicitly read and retain a new source version."""
        saved = await self.get(skill_id, project_id)
        require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "Skill configuration changed")
        definition = SkillSource.model_validate(saved["definition"])
        body, sha = await asyncio.to_thread(self.main_body, definition)
        metadata = self.metadata(body)
        definition = SkillSource.model_validate(definition.model_dump() | {key: metadata[key] for key in ("name", "description") if key in metadata})
        return await self._register(definition, body, sha, expected_revision=expected_revision)

    async def _manage(self, skill_id, project_id, expected_revision, *, enabled=None, remove=False):
        def save(db):
            saved = self.scoped(db, skill_id, project_id, include_removed=remove)
            definition = SkillSource.model_validate_json(saved["config_json"])
            if saved["removed"]:
                require(expected_revision in {saved["revision"], saved["revision"] - 1}, "REVISION_CONFLICT", "Skill configuration changed")
                return definition, saved["revision"], True
            require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "Skill configuration changed")
            definition.enabled = False if remove else enabled
            revision = saved["revision"] + 1
            db.execute("UPDATE skills SET config_json=?,revision=?,removed=?,updated_at=? WHERE id=?", (definition.model_dump_json(), revision, int(remove), now_ms(), skill_id))
            db.execute("INSERT INTO skill_versions SELECT skill_id,?,path,sha256,name,description,? FROM skill_versions WHERE skill_id=? AND revision=?", (revision, now_ms(), skill_id, saved["revision"]))
            event(db, "skill.removed" if remove else "skill.enabled", project_id=definition.project_id, data={"skill_id": skill_id, "revision": revision, "enabled": definition.enabled})
            return definition, revision, False
        definition, revision, reused = await self.host.store.call(save, transaction=True)
        if remove:
            self.definitions.pop(skill_id, None)
        else:
            self.definitions[skill_id] = (definition, revision)
        if not reused:
            self.host.notify()
        return {"skill_id": skill_id, "revision": revision, "enabled": definition.enabled, "removed": remove, **({"reused": reused} if remove else {})}

    async def set_enabled(self, skill_id: str, project_id: str, enabled: bool, expected_revision: int):
        return await self._manage(skill_id, project_id, expected_revision, enabled=enabled)

    async def remove(self, skill_id: str, project_id: str, expected_revision: int):
        """Detach the shared registration, retaining original files and old evidence."""
        return await self._manage(skill_id, project_id, expected_revision, remove=True)

    def pin(self, db, definition, project_id):
        pinned = []
        for binding in definition.skills:
            if binding.path is not None:
                pinned.append(binding)
                continue
            record = self.scoped(db, binding.id, project_id)
            source = SkillSource.model_validate_json(record["config_json"])
            require(source.enabled, "SKILL_DISABLED", "Registered Skill is disabled")
            revision = binding.revision or record["revision"]
            version = one(db, "SELECT path,name,description FROM skill_versions WHERE skill_id=? AND revision=?", (binding.id, revision))
            require(version and Path(version["path"]) == source.path, "SKILL_VERSION_UNAVAILABLE", "Requested Skill version is unavailable or its source directory moved")
            pinned.append(binding.model_copy(update={"path": source.path, "revision": revision, "name": version["name"], "description": version["description"]}))
        return definition.model_copy(update={"skills": tuple(pinned)})

    def authorize(self, binding, current_binding, project_id):
        current = self.definitions.get(binding.id)
        require(current and current[0].enabled and current[0].project_id in {None, project_id} and current[0].path == binding.path
            and (current_binding.path is None or current_binding.path == binding.path) and current_binding.revision in {None, binding.revision},
            "PERMISSION_DENIED", "Shared Skill is disabled, detached, moved, changed, or belongs to another project")

    async def body(self, skill_id, revision):
        record = await self.host.store.call(lambda db: one(db, "SELECT b.body,v.sha256 FROM skill_versions v JOIN skill_bodies b ON b.sha256=v.sha256 WHERE v.skill_id=? AND v.revision=?", (skill_id, revision)))
        require(record, "SKILL_VERSION_UNAVAILABLE", "The accepted Skill body version is unavailable")
        return record

    async def read(self, skill_id: str, project_id: str, *, path: str = "SKILL.md", revision: int | None = None, offset: int = 0, limit: int = 8192):
        require(0 <= offset < 2**63 and 1 <= limit <= 16384, "INVALID_REQUEST", "Use a bounded Skill text page")
        saved = await self.get(skill_id, project_id)
        definition = SkillSource.model_validate(saved["definition"])
        revision = saved["revision"] if revision is None else revision
        if path == "SKILL.md":
            version = await self.body(skill_id, revision)
            body, sha = version["body"], version["sha256"]
        else:
            require(revision == saved["revision"], "SKILL_VERSION_CHANGED", "Load a new reference only from the current source version")
            target = self.source_path(definition.path, path)
            require(target.stat().st_size <= 65536, "SKILL_TOO_LARGE", "Read a smaller Skill reference")
            body = await asyncio.to_thread(target.read_text, encoding="utf-8-sig")
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return {"skill_id": skill_id, "project_id": project_id, "path": path, "revision": revision, "sha256": sha, "text": body[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(body) else None}

    async def files(self, skill_id: str, project_id: str, *, path: str = "", after: str = "", limit: int = 25):
        require(1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded directory page")
        saved = await self.get(skill_id, project_id)
        root = Path(saved["definition"]["path"])
        directory = (root / path).resolve()
        require(directory.is_relative_to(root) and directory.is_dir(), "PERMISSION_DENIED", "Skill directory is outside its source")
        def page():
            entries = sorted((entry for entry in directory.iterdir() if entry.name > after), key=lambda entry: entry.name)[:limit + 1]
            return {"items": [{"name": entry.name, "path": str(entry.relative_to(root)), "directory": entry.is_dir()} for entry in entries[:limit]],
                    "after": entries[limit - 1].name if len(entries) > limit else None}
        return await asyncio.to_thread(page)
