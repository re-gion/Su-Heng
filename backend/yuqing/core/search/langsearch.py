from __future__ import annotations

import asyncio
import time
from collections import deque

import httpx

from yuqing.core.search.base import SearchParams, SearchResult


class LangSearchLimiter:
    def __init__(self, interval: float, *, max_calls_per_minute: int = 55):
        self.interval = interval
        self.max_calls_per_minute = max(1, max_calls_per_minute)
        self.last_call = 0.0
        self.calls: deque[float] = deque()
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                window_start = now - 60
                while self.calls and self.calls[0] <= window_start:
                    self.calls.popleft()
                delays = [
                    self.interval - (now - self.last_call),
                    self.blocked_until - now,
                ]
                if len(self.calls) >= self.max_calls_per_minute:
                    delays.append(self.calls[0] + 60 - now)
                delay = max(delays)
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                now = time.monotonic()
                self.last_call = now
                self.calls.append(now)
                return

    async def defer(self, seconds: float) -> None:
        async with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


class LangSearchProvider:
    name = "langsearch"
    capabilities = {"freshness", "domain_filter", "publish_time"}
    retry_managed = True

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        qps: float = 1.0,
        limiter: LangSearchLimiter | None = None,
    ):
        if not api_key:
            raise ValueError("LANGSEARCH_API_KEY 未配置")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        # 官方 1 QPS 按服务端时间窗口计数；留出 10% 抖动余量避免边界 429。
        self._limiter = limiter or LangSearchLimiter(1.1 / qps)

    async def _throttle(self) -> None:
        await self._limiter.acquire()

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "").strip()
        try:
            return min(60.0, max(0.1, float(raw)))
        except ValueError:
            return min(60.0, 15.0 * (2**attempt))

    async def search(self, params: SearchParams) -> list[SearchResult]:
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
        response: httpx.Response | None = None
        for attempt in range(3):
            await self._throttle()
            response = await self.client.post(
                "https://api.langsearch.com/v1/web-search",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code != 429 or attempt == 2:
                break
            await self._limiter.defer(self._retry_after(response, attempt))
        assert response is not None
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
                lang=params.lang,
                raw=item,
            )
            for item in items
            if item.get("url")
        ]
