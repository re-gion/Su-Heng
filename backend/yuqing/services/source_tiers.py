from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

SourceRole = Literal["authority", "party", "independent", "syndicated", "unknown"]


class TierGroup(BaseModel):
    tier: int
    role: SourceRole
    domains: list[str]


class TierConfig(BaseModel):
    groups: list[TierGroup]
    default_tier: int = 4
    default_role: SourceRole = "unknown"


class SourceTierClassifier:
    def __init__(self, config: TierConfig):
        self.config = config

    @classmethod
    def from_yaml(cls, path: Path) -> SourceTierClassifier:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(TierConfig.model_validate(raw))

    @classmethod
    def bundled(cls) -> SourceTierClassifier:
        return cls.from_yaml(Path(__file__).with_name("source_tiers.yaml"))

    def classify(self, domain: str) -> tuple[int, SourceRole, bool]:
        host = domain.lower().strip(".")
        for group in self.config.groups:
            if any(host == item or host.endswith(f".{item}") for item in group.domains):
                return group.tier, group.role, True
        return self.config.default_tier, self.config.default_role, False
