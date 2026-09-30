from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from yuqing.core.text_safety import repair_unicode_scalars


class SearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=10, ge=1, le=50)
    # Providers accept both relative windows and an exact day/range.  Keeping
    # the value as the provider's wire format avoids silently changing the
    # requested UTC date in an adapter.
    freshness: str = "noLimit"
    # Explicit historical dates may be a preferred investigation window rather
    # than a hard cutoff. Only callers with that contract opt into recovery.
    allow_freshness_fallback: bool = False
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    lang: str = "zh"
    region: str | None = None
    # Full text is an opt-in at the provider boundary.  It is deliberately
    # represented separately from fetched evidence/snapshots.
    contents_text: bool = True
    # The free LangSearch tier meters returned text tokens. Investigation uses
    # snippets there while keeping Exa full text available in the same chain.
    langsearch_contents_text: bool | None = None
    max_characters: int = Field(default=3000, ge=256, le=20_000)
    priority: Literal["normal", "critical"] = "normal"

    @model_validator(mode="after")
    def validate_freshness(self) -> SearchParams:
        if self.freshness in {"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}:
            return self
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?", self.freshness):
            values = self.freshness.split("..")
            try:
                dates = [datetime.strptime(value, "%Y-%m-%d") for value in values]
            except ValueError as exc:
                raise ValueError("freshness 日期无效") from exc
            if len(dates) == 2 and dates[0] > dates[1]:
                raise ValueError("freshness 日期范围必须按起止顺序排列")
            return self
        raise ValueError(
            "freshness 必须是 noLimit/oneDay/oneWeek/oneMonth/oneYear 或 YYYY-MM-DD 日期"
        )


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str
    summary: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None
    provider: str
    lang: str = "zh"
    # Provider text is useful context, but is not a direct HTTP snapshot and
    # must not be interpreted as evidence.fetch_status='fetched'.
    content_text: str | None = None
    content_origin: Literal["search_snippet", "provider_fulltext"] = "search_snippet"
    usage: dict[str, Any] | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def repair_provider_unicode(cls, value: Any) -> Any:
        repaired = repair_unicode_scalars(value)
        if isinstance(repaired, dict):
            content_text = repaired.get("content_text")
            origin = repaired.get("content_origin")
            if content_text and origin is None:
                repaired["content_origin"] = "provider_fulltext"
        return repaired

    @model_validator(mode="after")
    def validate_provider_content(self) -> SearchResult:
        if self.content_text is not None and not self.content_text.strip():
            self.content_text = None
        if self.content_text is not None and self.content_origin != "provider_fulltext":
            raise ValueError("provider 返回正文必须标记为 provider_fulltext")
        if self.content_origin == "provider_fulltext" and self.content_text is None:
            raise ValueError("provider_fulltext 必须包含 content_text")
        return self


class SearchProvider(Protocol):
    name: str
    capabilities: set[str]

    async def search(self, params: SearchParams) -> list[SearchResult]: ...
