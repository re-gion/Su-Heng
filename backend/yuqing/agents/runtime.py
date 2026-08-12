from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from yuqing.storage.models import ClaimRecord, EvidenceRecord


class InvestigationPlan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


class GeneratedClaim(BaseModel):
    text: str = Field(min_length=1)
    statement_kind: str = "fact"
    rumor_text: str | None = None
    correction_text: str | None = None
    evidence_ids: list[str] = Field(min_length=1, max_length=4)


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
