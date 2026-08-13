from __future__ import annotations

from typing import Any

import httpx

from yuqing.core.search.base import SearchParams, SearchResult


def _items(body: dict[str, Any], *paths: tuple[str, ...]) -> list[dict[str, Any]]:
    for path in paths:
        value: Any = body
        for key in path:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        if isinstance(value, list):
            return value
    return []


def _normalized(items: list[dict[str, Any]], provider: str) -> list[SearchResult]:
    result = []
    for item in items:
        url = item.get("url") or item.get("link")
        if not url:
            continue
        result.append(
            SearchResult(
                url=url,
                title=item.get("title") or item.get("name") or url,
                snippet=item.get("content")
                or item.get("snippet")
                or item.get("description")
                or "无摘要",
                summary=item.get("summary") or item.get("content"),
                published_at=item.get("publish_date")
                or item.get("date")
                or item.get("datePublished"),
                source_name=item.get("media") or item.get("source") or item.get("siteName"),
                provider=provider,
                raw=item,
            )
        )
    return result


class _HTTPProvider:
    capabilities = {"freshness", "publish_time"}

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError(f"{self.name} API key 未配置")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))


class ZhipuSearchProvider(_HTTPProvider):
    name = "zhipu"
    capabilities = {"freshness", "domain_filter", "publish_time"}

    async def search(self, params: SearchParams) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "search_engine": "search_std",
            "search_query": params.query,
            "count": params.top_k,
            "search_recency_filter": params.freshness,
        }
        if params.include_domains:
            payload["search_domain_filter"] = params.include_domains[0]
        response = await self.client.post(
            "https://open.bigmodel.cn/api/paas/v4/web_search",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        return _normalized(
            _items(response.json(), ("search_result",), ("data", "search_result")), self.name
        )


class QianfanSearchProvider(_HTTPProvider):
    name = "qianfan"
    capabilities = {"domain_filter", "publish_time"}

    async def search(self, params: SearchParams) -> list[SearchResult]:
        payload: dict[str, Any] = {"query": params.query, "search_source": "baidu_search_v2"}
        if params.include_domains:
            payload["sites"] = params.include_domains[:100]
        response = await self.client.post(
            "https://qianfan.baidubce.com/v2/ai_search/web_search",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        return _normalized(
            _items(response.json(), ("references",), ("data", "references")), self.name
        )[: params.top_k]


class TavilySearchProvider(_HTTPProvider):
    name = "tavily"

    async def search(self, params: SearchParams) -> list[SearchResult]:
        days = {"oneDay": 1, "oneWeek": 7, "oneMonth": 30, "oneYear": 365}.get(params.freshness)
        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "query": params.query,
            "max_results": params.top_k,
            "search_depth": "basic",
            "topic": "news" if days else "general",
        }
        if days:
            payload["days"] = days
        if params.include_domains:
            payload["include_domains"] = params.include_domains
        response = await self.client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        return _normalized(_items(response.json(), ("results",)), self.name)


class SerperSearchProvider(_HTTPProvider):
    name = "serper"

    async def search(self, params: SearchParams) -> list[SearchResult]:
        tbs = {"oneDay": "qdr:d", "oneWeek": "qdr:w", "oneMonth": "qdr:m", "oneYear": "qdr:y"}.get(
            params.freshness
        )
        payload: dict[str, Any] = {
            "q": params.query,
            "num": params.top_k,
            "gl": "cn",
            "hl": "zh-cn",
        }
        if tbs:
            payload["tbs"] = tbs
        response = await self.client.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        return _normalized(_items(response.json(), ("news",), ("organic",)), self.name)
