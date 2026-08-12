from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

Depth = Literal["quick", "standard", "deep"]
TaskStatus = Literal["queued", "running", "pausing", "paused", "stopping", "failed", "done"]
FetchStatus = Literal["discovered", "fetched", "fetch_failed"]
SourceRole = Literal["authority", "party", "independent", "syndicated", "unknown"]
StatementKind = Literal["fact", "rumor"]
Badge = Literal["verified", "unverified", "disputed", "refuted"]


class TaskCreate(BaseModel):
    event_query: str = Field(min_length=1, max_length=200)
    user_note: str | None = None
    depth: Depth = "standard"
    time_range: "TimeRange | None" = None

    @property
    def time_range_from(self) -> str | None:
        return self.time_range.from_ if self.time_range else None

    @property
    def time_range_to(self) -> str | None:
        return self.time_range.to if self.time_range else None


class TimeRange(BaseModel):
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class TaskRecord(BaseModel):
    id: str
    event_query: str
    user_note: str | None = None
    depth: Depth
    time_range_from: str | None = None
    time_range_to: str | None = None
    status: TaskStatus
    outer_round: int
    tokens_used: int
    cost_estimate: float
    created_at: str
    updated_at: str


class EvidenceCreate(BaseModel):
    task_id: str
    url: HttpUrl
    title: str = Field(min_length=1)
    snippet: str | None = None
    summary: str | None = None
    source_name: str | None = None
    publisher_entity: str | None = None
    origin_url: str | None = None
    source_role: SourceRole = "unknown"
    source_tier: int = Field(default=4, ge=1, le=5)
    published_at: str | None = None
    retrieval_query: str | None = None
    provider: str | None = None
    fetch_status: FetchStatus = "discovered"
    fetched_at: str | None = None
    content_text: str | None = None
    snapshot_path: str | None = None
    content_sha256: str | None = None
    lang: str | None = "zh"
    extra: dict | None = None


class EvidenceRecord(BaseModel):
    pk: str
    task_id: str
    local_id: str
    url: str
    title: str
    snippet: str | None
    source_name: str | None
    source_domain: str
    publisher_entity: str | None
    source_role: SourceRole
    source_tier: int
    published_at: str | None
    fetch_status: FetchStatus
    content_text: str | None
    snapshot_path: str | None
    content_sha256: str | None
    provider: str | None


class QuoteCreate(BaseModel):
    evidence_id: str
    quote: str | None = None
    quote_type: Literal["verbatim", "paraphrase", "snippet"] = "paraphrase"


class ClaimCreate(BaseModel):
    task_id: str
    text: str = Field(min_length=1)
    statement_kind: StatementKind = "fact"
    rumor_text: str | None = None
    correction_text: str | None = None
    agent: str
    round: int = Field(default=1, ge=1)
    section: str = "fact_check"
    is_editorial: bool = False
    is_key: bool = True
    is_key_reason: str | None = None
    evidence_ids: list[str] = Field(min_length=1, max_length=6)
    quotes: list[QuoteCreate] = []


class ClaimRecord(BaseModel):
    pk: str
    task_id: str
    local_id: str
    text: str
    statement_kind: StatementKind
    rumor_text: str | None
    correction_text: str | None
    agent: str
    round: int
    section: str | None
    badge: Badge | None
    verdict: str | None
    verification_state: str
    independent_sources: int = 0
    max_source_tier: int | None = None
    evidence_ids: list[str] = []
