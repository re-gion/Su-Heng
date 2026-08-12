from __future__ import annotations

from pathlib import Path

import frontmatter
from pydantic import BaseModel, Field, ValidationError

from yuqing.core.llm.roles import LLMRole


class AgentDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,31}$")
    label: str
    model_role: LLMRole
    tools: list[str] = []
    skills: list[str] = []
    max_inner_rounds: int = Field(default=3, ge=1, le=8)
    token_budget: int = Field(default=60000, ge=1000)
    enabled: bool = True
    system_prompt: str
    source_path: str
    warnings: list[str] = []


def load_definitions(
    directory: Path, *, known_tools: set[str], skills_directory: Path
) -> tuple[list[AgentDefinition], list[str]]:
    loaded: list[AgentDefinition] = []
    errors: list[str] = []
    names: set[str] = set()
    for path in sorted(directory.glob("*.md")):
        post = frontmatter.load(path)
        metadata = dict(post.metadata)
        warnings: list[str] = []
        tools = [name for name in metadata.get("tools", []) if name in known_tools]
        for name in set(metadata.get("tools", [])) - set(tools):
            warnings.append(f"未知工具 {name}，已剔除")
        skills = [
            name
            for name in metadata.get("skills", [])
            if (skills_directory / f"{name}.md").is_file()
        ]
        for name in set(metadata.get("skills", [])) - set(skills):
            warnings.append(f"技能 {name} 不存在，已剔除")
        metadata.update(
            label=metadata.get("label") or metadata.get("name") or path.stem,
            tools=tools,
            skills=skills,
            max_inner_rounds=max(1, min(8, int(metadata.get("max_inner_rounds", 3)))),
            token_budget=int(metadata.get("token_budget", 60000))
            if int(metadata.get("token_budget", 60000)) >= 1000
            else 60000,
            system_prompt=post.content.strip(),
            source_path=str(path),
            warnings=warnings,
        )
        try:
            definition = AgentDefinition.model_validate(metadata)
        except (ValidationError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if definition.name in names:
            errors.append(f"{path.name}: name 重复")
            continue
        names.add(definition.name)
        loaded.append(definition)
    return loaded, errors
