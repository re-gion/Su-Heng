from __future__ import annotations

import httpx
import trafilatura

from yuqing.core.fetch.base import FetchResult
from yuqing.core.fetch.security import validate_public_url


class FetchError(RuntimeError):
    pass


class BuiltinFetchProvider:
    def __init__(
        self, *, client: httpx.AsyncClient | None = None, max_bytes: int = 5 * 1024 * 1024
    ):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            headers={"User-Agent": "YuQingAgent/0.1 (+public evidence research)"},
            follow_redirects=False,
        )
        self.max_bytes = max_bytes

    async def fetch(self, url: str) -> FetchResult:
        current = validate_public_url(url)
        for _ in range(6):
            async with self.client.stream("GET", current) as response:
                if response.is_redirect:
                    target = response.headers.get("location")
                    if not target:
                        raise FetchError("重定向缺少 Location")
                    current = validate_public_url(str(response.url.join(target)))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    raise FetchError(f"不支持的 Content-Type: {content_type}")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > self.max_bytes:
                            raise FetchError("页面超过 5MB 上限")
                    except ValueError:
                        pass
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > self.max_bytes:
                        raise FetchError("页面超过 5MB 上限")
                html = bytes(payload).decode(response.encoding or "utf-8", errors="replace")
                text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
                if not text.strip():
                    raise FetchError("未能抽取有效正文")
                return FetchResult(
                    url=str(response.url), html=html, content_text=text, content_type=content_type
                )
        raise FetchError("重定向次数过多")
