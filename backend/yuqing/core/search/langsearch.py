from __future__ import annotations

import asyncio
import time

import httpx

from yuqing.core.search.base import SearchParams, SearchResult


class LangSearchProvider:
    name = "langsearch"
    capabilities = {"freshness", "domain_filter", "publish_time"}

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None, qps: float = 1.0):
        if not api_key:
            raise ValueError("LANGSEARCH_API_KEY 未配置")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self._interval = 1 / qps
        self._last_call = 0.0
        self._rate_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._rate_lock:
            delay = self._interval - (time.monotonic() - self._last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = time.monotonic()

    async def search(self, params: SearchParams) -> list[SearchResult]:
        await self._throttle()
        payload = {
            "query": params.query,
            "count": params.top_k,
            "freshness": params.freshness,
            "summary": True,
        }
        if params.include_domains:
            payload["include_domains"] = params.include_domains
        if params.exclude_domains:
            payload["exclude_domains"] = params.exclude_domains
        response = await self.client.post(
            "https://api.langsearch.com/v1/web-search",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        items = (
            body.get("data", {})
            .get("webPages", {})
            .get("value", body.get("data", {}).get("results", []))
        )
        return [
            SearchResult(
                url=item["url"],
                title=item.get("name") or item.get("title") or item["url"],
                snippet=item.get("snippet") or item.get("summary") or "无摘要",
                summary=item.get("summary"),
                published_at=item.get("datePublished") or item.get("published_at"),
                source_name=item.get("siteName") or item.get("source_name"),
                provider=self.name,
                raw=item,
            )
            for item in items
            if item.get("url")
        ]
