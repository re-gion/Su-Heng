from typing import Protocol

from pydantic import BaseModel


class FetchResult(BaseModel):
    url: str
    html: str
    content_text: str
    content_type: str


class FetchError(RuntimeError):
    """A classified failure from a fetch provider.

    ``kind`` is deliberately small and provider-independent so a chain can make
    a conservative fallback decision without parsing human-facing messages.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
        retry_exhausted: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_exhausted = retry_exhausted
        self.status_code = status_code


class FetchProvider(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
