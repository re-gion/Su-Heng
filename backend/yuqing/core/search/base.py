from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from yuqing.core.text_safety import repair_unicode_scalars


class SearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=10, ge=1, le=10)
    freshness: Literal["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"] = "noLimit"
    include_domains: list[str] = []
    exclude_domains: list[str] = []
    lang: str = "zh"
    region: str | None = None


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str
    summary: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None
    provider: str
    lang: str = "zh"
    raw: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def repair_provider_unicode(cls, value: Any) -> Any:
        return repair_unicode_scalars(value)


class SearchProvider(Protocol):
    name: str
    capabilities: set[str]

    async def search(self, params: SearchParams) -> list[SearchResult]: ...
