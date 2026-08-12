from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

LLMRole = Literal[
    "analyst_a", "analyst_b", "analyst_c", "moderator", "verifier", "reporter", "utility"
]
ROLES: tuple[LLMRole, ...] = (
    "analyst_a",
    "analyst_b",
    "analyst_c",
    "moderator",
    "verifier",
    "reporter",
    "utility",
)


class LLMRoleConfig(BaseModel):
    api_key: str = Field(min_length=1)
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int | None = None
    timeout: float = 300


def load_role_config(role: LLMRole, environ: Mapping[str, str] | None = None) -> LLMRoleConfig:
    values = environ or os.environ
    prefix = f"LLM_{role.upper()}_"

    def inherited(field: str, fallback: str = "") -> str:
        return (
            values.get(prefix + field, "").strip()
            or values.get("DEFAULT_" + field, "").strip()
            or fallback
        )

    api_key = inherited("API_KEY")
    if not api_key:
        raise ValueError(f"{role} 未配置 API key；请设置 DEFAULT_API_KEY 或 {prefix}API_KEY")
    return LLMRoleConfig(
        api_key=api_key,
        base_url=inherited("BASE_URL", "https://api.deepseek.com/v1"),
        model=inherited("MODEL", "deepseek-chat"),
        temperature=0 if role == "verifier" else 0.3,
    )
