from typing import Protocol

from pydantic import BaseModel


class FetchResult(BaseModel):
    url: str
    html: str
    content_text: str
    content_type: str


class FetchProvider(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
