"""Versioned, local-only methods shared by runtime and public capability cards."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

import frontmatter
import yaml
from pydantic import BaseModel, Field

from yuqing.agents.loader import AgentDefinition, load_definitions

SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
BUILTIN_TOOLS = {
    "read_skill",
    "web_search",
    "fetch_page",
    "evidence_write",
    "claim_write",
    "forum_post",
    "forum_read",
    "verify_claim",
    "report_write",
    "comment_read",
    "evidence_search",
    "hotlist_query",
    "dataset_query",
}


class SkillDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    roles: list[str] = Field(min_length=1)
    stages: list[str] = Field(default_factory=list)
    output: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=20, max_length=16000)
    digest: str


class SkillCatalog:
    def __init__(self, definitions: list[AgentDefinition], directory: Path):
        self.agents = {item.model_role: item for item in definitions}
        self.skills: dict[str, SkillDefinition] = {}
        self.errors: list[str] = []
        for path in sorted(directory.glob("*.md")):
            try:
                if not SKILL_ID.fullmatch(path.stem) or path.is_symlink():
                    raise ValueError("invalid skill path")
                raw = path.read_text(encoding="utf-8")
                if len(raw) > 20000:
                    raise ValueError("skill too large")
                post = frontmatter.loads(raw)
                skill = SkillDefinition(
                    **{**post.metadata, "id": path.stem},
                    body=post.content.strip(),
                    digest=hashlib.sha256(raw.encode()).hexdigest()[:16],
                )
                self.skills[skill.id] = skill
            except (ValueError, TypeError, OSError, yaml.YAMLError):
                self.errors.append(f"技能 {path.stem} 未通过方法包校验")

    def for_role(self, role: str, *, shared: bool = False) -> list[SkillDefinition]:
        agent = self.agents.get(role)
        if agent is None or not agent.enabled:
            return []
        return [
            skill
            for name in agent.skills
            if (skill := self.skills.get(name)) is not None
            and (("shared" in skill.roles) if shared else (role in skill.roles))
        ]

    def candidates(self, role: str, stage: str) -> list[SkillDefinition]:
        skills = self.for_role(role)
        matched = [item for item in skills if stage in item.stages]
        if matched:
            return matched
        generic = [item for item in skills if "*" in item.stages]
        if generic:
            return generic
        agent = self.agents.get(role)
        return [item for item in skills if agent and item.id == agent.main_skill]

    def public(self) -> dict:
        return {
            "agents": [
                {
                    "id": agent.name,
                    "role": role,
                    "label": agent.label,
                    "description": agent.description,
                    "enabled": agent.enabled,
                    "main_skill": agent.main_skill,
                    "selection_mode": "model"
                    if role in {"analyst_a", "analyst_b", "analyst_c"}
                    else "work_order",
                    "skills": [
                        {**skill.model_dump(exclude={"roles", "stages"}), "shared": shared}
                        for shared in (False, True)
                        for skill in self.for_role(role, shared=shared)
                    ],
                }
                for role, agent in self.agents.items()
            ],
            "errors": self.errors,
            "read_only": True,
        }


@lru_cache(maxsize=1)
def builtin_catalog() -> SkillCatalog:
    root = Path(__file__).parent
    definitions, errors = load_definitions(
        root / "definitions", known_tools=BUILTIN_TOOLS, skills_directory=root / "skills"
    )
    catalog = SkillCatalog(definitions, root / "skills")
    catalog.errors.extend(errors)
    for agent in definitions:
        if not any(item.id == agent.main_skill for item in catalog.for_role(agent.model_role)):
            catalog.errors.append(f"{agent.label} 缺少有效主技能")
    return catalog
