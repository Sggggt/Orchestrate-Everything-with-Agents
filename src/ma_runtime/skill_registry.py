"""Explicit directory registration with immutable, progressively read snapshots."""

import asyncio
import re
from pathlib import Path

import yaml

from .errors import RuntimeFault, require
from .models import AgentSkill, content_hash, json_dump, now_ms
from .store import rows


def read_directory(directory):
    root = Path(directory).expanduser()
    require(root.is_absolute() and root.is_dir(), "INVALID_REQUEST", "Choose an existing absolute Skill directory")
    root = root.resolve()
    document = root / "SKILL.md"
    require(document.is_file() and document.resolve().is_relative_to(root), "INVALID_SKILL", "Skill directory must contain SKILL.md")
    require(document.stat().st_size <= 65536, "INVALID_SKILL", "SKILL.md exceeds 64 KiB")
    try:
        content = document.read_text(encoding="utf-8-sig")
        lines = content.splitlines()
        require(lines and lines[0] == "---", "INVALID_SKILL", "SKILL.md requires YAML frontmatter")
        end = lines.index("---", 1)
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        raise RuntimeFault("INVALID_SKILL", "Cannot read valid UTF-8 SKILL.md frontmatter") from None
    require(isinstance(metadata, dict), "INVALID_SKILL", "Skill frontmatter must be a mapping")
    name, description = metadata.get("name"), metadata.get("description")
    require(isinstance(name, str) and len(name) <= 64 and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            and name == root.name, "INVALID_SKILL", "Skill name must be a lowercase hyphenated directory name within 64 characters")
    require(isinstance(description, str) and bool(description.strip()) and len(description) <= 1024,
            "INVALID_SKILL", "Skill description must contain 1..1024 characters")
    for key in ("license", "allowed-tools", "compatibility"):
        require(key not in metadata or isinstance(metadata[key], str), "INVALID_SKILL", f"{key} must be text")
    require("compatibility" not in metadata or 0 < len(metadata["compatibility"]) <= 500,
            "INVALID_SKILL", "compatibility must contain 1..500 characters")
    require(isinstance(metadata.get("metadata", {}), dict) and all(isinstance(k, str) and isinstance(v, str)
            for k, v in metadata.get("metadata", {}).items()), "INVALID_SKILL", "metadata must map strings to strings")
    resources, count, size = {}, 0, 0
    # Registration/refresh alone scans the source. Binary assets stay in place;
    # the runtime reads bounded text and never executes a bundled script.
    for path in root.rglob("*"):
        count += 1
        require(count <= 512, "INVALID_SKILL", "Skill package exceeds 512 entries")
        require(path.resolve().is_relative_to(root), "INVALID_SKILL", "Skill resource escapes its directory")
        require(not path.is_symlink() and not path.is_junction(), "INVALID_SKILL", "Skill resources cannot be links")
        if not path.is_file() or path == document:
            continue
        if path.stat().st_size > 262144:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            continue
        if "\x00" in text:
            continue
        size += len(text.encode("utf-8"))
        require(len(resources) < 32 and size <= 262144, "INVALID_SKILL", "Skill text resources exceed 32 files or 256 KiB")
        resources[path.relative_to(root).as_posix()] = text
    revision = content_hash({"content": content, "resources": resources})
    return str(root), AgentSkill(id=name, name=name, description=description, content=content,
                                resources=resources, metadata=metadata, revision=revision)


class SkillRegistry:
    def __init__(self, runtime):
        self.rt, self.entries = runtime, {}

    async def open(self):
        for row in await self.rt.store.call(lambda db: rows(db, "SELECT * FROM skill_registry")):
            self.entries[row.id] = {"directory": row.directory, "enabled": bool(row.enabled),
                                    "skill": AgentSkill.model_validate_json(row.skill_json), "updated_at": row.updated_at}

    def entry(self, skill_id):
        value = self.entries.get(skill_id)
        require(value is not None, "NOT_FOUND", "Skill is not registered")
        return value

    def summary(self, skill_id):
        entry = self.entry(skill_id)
        skill = entry["skill"]
        return {"id": skill.id, "name": skill.name, "description": skill.description,
                "directory": entry["directory"], "enabled": entry["enabled"], "revision": skill.revision,
                "metadata": skill.metadata, "resource_count": len(skill.resources), "updated_at": entry["updated_at"],
                "bound_agents": [config.id for config in self.rt.agent_configs.values()
                                 if any(b.skill_id == skill_id for b in config.skill_bindings)]}

    async def list(self, cursor=None, limit=50):
        limit = max(1, min(int(limit), 200))
        ids = sorted(key for key in self.entries if key > (cursor or ""))
        selected = ids[:limit]
        return {"items": [self.summary(key) for key in selected], "has_more": len(ids) > limit,
                "cursor": selected[-1] if selected else cursor}

    async def get(self, skill_id):
        return {**self.summary(skill_id), "resources": sorted(self.entry(skill_id)["skill"].resources)}

    async def register_directory(self, directory, enabled=True):
        self.rt.admit()
        require(isinstance(enabled, bool), "INVALID_REQUEST", "enabled must be boolean")
        root, skill = await asyncio.to_thread(read_directory, directory)
        async with self.rt.configuration_lock:
            old = self.entries.get(skill.id)
            require(old is None or old["directory"] == root, "SKILL_CONFLICT", "Skill ID is registered from another directory")
            for config in self.rt.agent_configs.values():
                if any(binding.skill_id == skill.id for binding in config.skill_bindings):
                    self.validate_agent(config, replacement=skill)
            stamp = now_ms()
            await self.rt.store.call(lambda db: db.execute(
                "INSERT INTO skill_registry VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET directory=excluded.directory,enabled=excluded.enabled,skill_json=excluded.skill_json,updated_at=excluded.updated_at",
                (skill.id, root, int(enabled), json_dump(skill), stamp)), transaction=True)
            self.entries[skill.id] = {"directory": root, "enabled": bool(enabled), "skill": skill, "updated_at": stamp}
        self.rt.notify()
        return await self.get(skill.id)

    async def refresh(self, skill_id):
        entry = self.entry(skill_id)
        return await self.register_directory(entry["directory"], entry["enabled"])

    async def set_enabled(self, skill_id, enabled):
        self.rt.admit()
        require(isinstance(enabled, bool), "INVALID_REQUEST", "enabled must be boolean")
        async with self.rt.configuration_lock:
            entry = self.entry(skill_id)
            stamp = now_ms()
            await self.rt.store.call(lambda db: db.execute("UPDATE skill_registry SET enabled=?,updated_at=? WHERE id=?",
                                     (int(enabled), stamp, skill_id)), transaction=True)
            entry.update(enabled=enabled, updated_at=stamp)
        self.rt.notify()
        return await self.get(skill_id)

    async def read(self, skill_id, path="SKILL.md"):
        from .profiles import read_skill
        return read_skill((self.entry(skill_id)["skill"],), skill_id, path)

    async def delete(self, skill_id):
        self.rt.admit()
        async with self.rt.configuration_lock:
            self.entry(skill_id)
            require(not self.summary(skill_id)["bound_agents"], "SKILL_IN_USE", "Unlink this Skill from Agents before removing its registration")
            await self.rt.store.call(lambda db: db.execute("DELETE FROM skill_registry WHERE id=?", (skill_id,)), transaction=True)
            self.entries.pop(skill_id)
        self.rt.notify()
        return {"deleted": True, "source_preserved": True}

    def for_agent(self, config):
        if config is None:
            return ()
        skills = list(config.skills)
        for binding in config.skill_bindings:
            entry = self.entry(binding.skill_id)
            if entry["enabled"]:
                skills.append(entry["skill"])
        return tuple(skills)

    def validate_bindings(self, bindings):
        for binding in bindings:
            self.entry(binding.skill_id)

    def validate_agent(self, config, replacement=None):
        skills = list(config.skills)
        for binding in config.skill_bindings:
            skill = replacement if replacement is not None and binding.skill_id == replacement.id else self.entry(binding.skill_id)["skill"]
            skills.append(skill)
        require(sum(len(json_dump(skill).encode("utf-8")) for skill in skills) <= 524288,
                "SKILL_LIMIT", "Agent Skill snapshots exceed 512 KiB; link fewer or smaller packages")
