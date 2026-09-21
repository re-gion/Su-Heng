from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from yuqing.storage.models import ClaimRecord, EvidenceRecord, StatementKind


class SearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    language: str = Field(default="zh", min_length=2, max_length=15)
    region: str = Field(default="CN", min_length=2, max_length=8)


class InvestigationPlan(BaseModel):
    queries: list[SearchQuery] = Field(min_length=1, max_length=6)

    @field_validator("queries", mode="before")
    @classmethod
    def normalize_queries(cls, value):
        return [
            {"query": item, "language": "zh", "region": "CN"} if isinstance(item, str) else item
            for item in value
        ]


class GeneratedClaim(BaseModel):
    text: str = Field(min_length=1)
    statement_kind: StatementKind = "fact"
    rumor_text: str | None = None
    correction_text: str | None = None
    evidence_ids: list[str] = Field(min_length=1, max_length=4)
    analysis_data: dict[str, Any] = Field(default_factory=dict)


class Reflection(BaseModel):
    new_key_findings: list[str]
    remaining_gaps: list[str]
    next_queries: list[str]
    should_continue: bool
    reason: str


class InvestigationAgent(Protocol):
    async def plan(self, event_query: str) -> InvestigationPlan: ...

    async def summarize(
        self, event_query: str, evidence: list[EvidenceRecord]
    ) -> list[GeneratedClaim]: ...

    async def reflect(self, event_query: str, claims: list[ClaimRecord]) -> Reflection: ...
