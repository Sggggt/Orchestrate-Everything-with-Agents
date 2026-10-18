"""Per-Agent instruction documents and progressively disclosed skills."""

from .errors import RuntimeFault, require
from .models import AgentSkill


def skill_from_markdown(skill_id, markdown, *, enabled=True):
    require(
        isinstance(markdown, str) and len(markdown.encode("utf-8")) <= 65536,
        "INVALID_REQUEST",
        "SKILL.md must be UTF-8 text within 64 KiB",
    )
    metadata = {}
    if markdown.startswith("---\n") or markdown.startswith("---\r\n"):
        import yaml

        lines = markdown.splitlines()
        try:
            end = lines[1:].index("---") + 1
            metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
        except (ValueError, yaml.YAMLError):
            raise RuntimeFault(
                "INVALID_REQUEST", "SKILL.md contains invalid YAML front matter"
            ) from None
        require(
            isinstance(metadata, dict),
            "INVALID_REQUEST",
            "Skill front matter must be a mapping",
        )
    name = metadata.get("name", skill_id)
    description = metadata.get("description", "")
    require(
        isinstance(name, str) and isinstance(description, str),
        "INVALID_REQUEST",
        "Skill name and description must be text",
    )
    return AgentSkill(
        id=skill_id,
        name=name,
        description=description,
        content=markdown,
        enabled=enabled,
    )


def skill_catalog(skills):
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "path": f"skills/{s.id}/SKILL.md",
            **({"revision": s.revision} if s.revision else {}),
        }
        for s in skills
        if s.enabled
    ]


def read_skill(skills, skill_id, path="SKILL.md"):
    skill = next((s for s in skills if s.id == skill_id and s.enabled), None)
    require(
        skill is not None,
        "PERMISSION_DENIED",
        "This skill is not enabled for the current Agent",
    )
    require(
        path == "SKILL.md" or path in skill.resources,
        "NOT_FOUND",
        "Resource is not part of this skill",
    )
    return {
        "skill_id": skill.id,
        "path": f"skills/{skill.id}/{path}",
        "content": skill.content if path == "SKILL.md" else skill.resources[path],
    }
