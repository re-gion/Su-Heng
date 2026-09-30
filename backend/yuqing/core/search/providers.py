from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from yuqing.core.search.base import SearchParams, SearchResult

_DATETIME_ADAPTER = TypeAdapter(datetime)


def _items(body: dict[str, Any], *paths: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read the result list from the small number of provider response shapes."""

    for path in paths:
        value: Any = body
        for key in path:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return "\n".join(parts) or None
    return None


def _published_at_value(item: dict[str, Any]) -> datetime | None:
    value = (
        item.get("publish_date")
        or item.get("published_date")
        or item.get("datePublished")
        or item.get("publishedDate")
        or item.get("date")
    )
    if value is None:
        return None
    try:
        return _DATETIME_ADAPTER.validate_python(value)
    except (ValidationError, TypeError, ValueError):
        # Search APIs, especially Serper, may return display-oriented relative
        # values such as "5小时前" or "3 days ago".  They lack a trustworthy
        # timezone/reference instant, so retain them only in raw provider data.
        return None


def _normalized(
    items: list[dict[str, Any]],
    provider: str,
    lang: str = "zh",
    *,
    usage: dict[str, Any] | None = None,
    provider_metadata: dict[str, Any] | None = None,
    fulltext_keys: tuple[str, ...] = (),
    max_content_chars: int | None = None,
) -> list[SearchResult]:
    result: list[SearchResult] = []
    for item in items:
        url = item.get("url") or item.get("link")
        if not isinstance(url, str) or not url.strip():
            continue
        fulltext = next((_text_value(item.get(key)) for key in fulltext_keys), None)
        if fulltext and max_content_chars is not None:
            fulltext = fulltext[:max_content_chars]
        snippet = (
            _text_value(item.get("content"))
            or _text_value(item.get("snippet"))
            or _text_value(item.get("description"))
            or fulltext
            or "无摘要"
        )
        result.append(
            SearchResult(
                url=url,
                title=_text_value(item.get("title")) or _text_value(item.get("name")) or url,
                snippet=snippet,
                summary=_text_value(item.get("summary")),
                published_at=_published_at_value(item),
                source_name=_text_value(item.get("media"))
                or _text_value(item.get("source"))
                or _text_value(item.get("siteName"))
                or _text_value(item.get("website"))
                or _text_value(item.get("author")),
                provider=provider,
                lang=str(item.get("language") or item.get("lang") or lang),
                content_text=fulltext,
                content_origin=("provider_fulltext" if fulltext else "search_snippet"),
                usage=usage,
                provider_metadata=provider_metadata or {},
                raw=item,
            )
        )
    return result


class _HTTPProvider:
    capabilities = {"freshness", "publish_time"}

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ):
        if not api_key:
            raise ValueError(f"{self.name} API key 未配置")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        if base_url is not None:
            self.base_url = base_url.rstrip("/")


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
            _items(response.json(), ("search_result",), ("data", "search_result")),
            self.name,
            params.lang,
        )[: params.top_k]


class QianfanSearchProvider(_HTTPProvider):
    """Baidu Qianfan Web Search (not the AI answer-generation endpoint)."""

    name = "qianfan"
    capabilities = {"freshness", "domain_filter", "publish_time"}
    endpoint = "https://qianfan.baidubce.com/v2/ai_search/web_search"

    @staticmethod
    def _recency(freshness: str) -> str | None:
        return {"oneWeek": "week", "oneMonth": "month", "oneYear": "year"}.get(freshness)

    @staticmethod
    def _date_range(freshness: str) -> dict[str, str] | None:
        if ".." in freshness:
            start, end = freshness.split("..", 1)
            return {"gte": start, "lte": end}
        if len(freshness) == 10 and freshness[4] == "-":
            return {"gte": freshness, "lte": freshness}
        if freshness == "oneDay":
            return {"gte": "now-1d/d", "lte": "now/d"}
        return None

    async def search(self, params: SearchParams) -> list[SearchResult]:
        search_filter: dict[str, Any] = {}
        match: dict[str, Any] = {}
        if params.include_domains:
            match["site"] = params.include_domains[:100]
        if match:
            search_filter["match"] = match
        if params.exclude_domains:
            search_filter["block_websites"] = params.exclude_domains[:100]
        date_range = self._date_range(params.freshness)
        if date_range:
            search_filter["range"] = {"page_time": date_range}

        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": params.query}],
            "search_source": "baidu_search_v2",
            "edition": "standard",
            "resource_type_filter": [{"type": "web", "top_k": params.top_k}],
        }
        recency = self._recency(params.freshness)
        if recency:
            payload["search_recency_filter"] = recency
        if search_filter:
            payload["search_filter"] = search_filter
        response = await self.client.post(
            getattr(self, "base_url", self.endpoint),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        references = _items(body, ("references",), ("data", "references"))
        metadata = {
            key: body[key] for key in ("request_id", "requestId", "code", "message") if key in body
        }
        return _normalized(
            references,
            self.name,
            params.lang,
            provider_metadata=metadata,
        )[: params.top_k]


class BochaSearchProvider(_HTTPProvider):
    name = "bocha"
    capabilities = {"freshness", "domain_filter", "publish_time"}
    endpoint = "https://api.bochaai.com/v1/web-search"

    async def search(self, params: SearchParams) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": params.query,
            "count": params.top_k,
            "freshness": params.freshness,
            "summary": True,
        }
        # Bocha's Web Search API uses pipe-separated include/exclude fields.
        if params.include_domains:
            payload["include"] = "|".join(params.include_domains)
        if params.exclude_domains:
            payload["exclude"] = "|".join(params.exclude_domains)
        response = await self.client.post(
            getattr(self, "base_url", self.endpoint),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        metadata = {key: body[key] for key in ("code", "log_id", "msg") if key in body}
        return _normalized(
            _items(body, ("data", "webPages", "value")),
            self.name,
            params.lang,
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else None,
            provider_metadata=metadata,
        )[: params.top_k]


def _exa_dates(freshness: str) -> dict[str, str]:
    if freshness == "noLimit":
        return {}
    if ".." in freshness:
        start, end = freshness.split("..", 1)
        return {"startPublishedDate": start, "endPublishedDate": end}
    if len(freshness) == 10 and freshness[4] == "-":
        return {"startPublishedDate": freshness, "endPublishedDate": freshness}
    days = {"oneDay": 1, "oneWeek": 7, "oneMonth": 30, "oneYear": 365}.get(freshness)
    if days is None:
        return {}
    end = date.today()
    return {
        "startPublishedDate": (end - timedelta(days=days)).isoformat(),
        "endPublishedDate": end.isoformat(),
    }


class ExaSearchProvider(_HTTPProvider):
    name = "exa"
    capabilities = {"freshness", "domain_filter", "publish_time"}
    endpoint = "https://api.exa.ai/search"

    async def search(self, params: SearchParams) -> list[SearchResult]:
        payload: dict[str, Any] = {"query": params.query, "numResults": params.top_k}
        payload.update(_exa_dates(params.freshness))
        if params.include_domains:
            payload["includeDomains"] = params.include_domains
        if params.exclude_domains:
            payload["excludeDomains"] = params.exclude_domains
        if params.contents_text:
            # Bound text at Exa as well as locally: full pages are billed and
            # only their lead is used by the first relevance check.
            payload["contents"] = {"text": {"maxCharacters": params.max_characters}}
        response = await self.client.post(
            getattr(self, "base_url", self.endpoint),
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage")
        metadata = {key: body[key] for key in ("requestId", "request_id") if key in body}
        return _normalized(
            _items(body, ("results",), ("data", "results")),
            self.name,
            params.lang,
            usage=usage if isinstance(usage, dict) else None,
            provider_metadata=metadata,
            fulltext_keys=("text",),
            max_content_chars=params.max_characters,
        )[: params.top_k]


class TavilySearchProvider(_HTTPProvider):
    name = "tavily"
    capabilities = {"freshness", "domain_filter", "publish_time"}

    async def search(self, params: SearchParams) -> list[SearchResult]:
        time_range = {
            "oneDay": "day",
            "oneWeek": "week",
            "oneMonth": "month",
            "oneYear": "year",
        }.get(params.freshness)
        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "query": params.query,
            "max_results": params.top_k,
            "search_depth": "basic",
            "topic": "news" if time_range else "general",
        }
        if time_range:
            payload["time_range"] = time_range
        if params.include_domains:
            payload["include_domains"] = params.include_domains
        if params.exclude_domains:
            payload["exclude_domains"] = params.exclude_domains
        response = await self.client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        body = response.json()
        return _normalized(
            _items(body, ("results",)),
            self.name,
            params.lang,
            fulltext_keys=("raw_content",),
        )


class SerperSearchProvider(_HTTPProvider):
    name = "serper"
    capabilities = {"freshness", "domain_filter", "publish_time"}

    @staticmethod
    def _locale(params: SearchParams) -> tuple[str, str]:
        language = params.lang.lower()
        base_language = language.split("-", 1)[0]
        defaults = {
            "zh": ("cn", "zh-cn"),
            "en": ("us", "en"),
            "ja": ("jp", "ja"),
            "ko": ("kr", "ko"),
            "de": ("de", "de"),
            "fr": ("fr", "fr"),
            "es": ("es", "es"),
        }
        default_gl, default_hl = defaults.get(base_language, ("us", language))
        if not params.region:
            return default_gl, default_hl
        region = params.region.replace("_", "-").lower()
        region_parts = region.split("-")
        gl = region_parts[-1] if len(region_parts) > 1 else region_parts[0]
        return gl, default_hl

    async def search(self, params: SearchParams) -> list[SearchResult]:
        tbs = {
            "oneDay": "qdr:d",
            "oneWeek": "qdr:w",
            "oneMonth": "qdr:m",
            "oneYear": "qdr:y",
        }.get(params.freshness)
        gl, hl = self._locale(params)
        query = params.query
        # Serper has no first-class domain arrays. Keep these constraints in
        # the provider query as a hint; SearchChain applies the hard filter.
        if params.include_domains:
            query = f"{query} " + " ".join(f"site:{domain}" for domain in params.include_domains)
        if params.exclude_domains:
            query = f"{query} " + " ".join(f"-site:{domain}" for domain in params.exclude_domains)
        payload: dict[str, Any] = {"q": query, "num": params.top_k, "gl": gl, "hl": hl}
        if tbs:
            payload["tbs"] = tbs
        response = await self.client.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        return _normalized(_items(response.json(), ("news",), ("organic",)), self.name, params.lang)


# Short aliases are useful for integrations that use the provider names as
# classes, while the explicit SearchProvider names remain the public API.
BochaProvider = BochaSearchProvider
ExaProvider = ExaSearchProvider
