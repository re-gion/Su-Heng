from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

from yuqing.core.search.base import SearchParams, SearchResult
from yuqing.core.search.providers import _published_at_value


class YuanbaoSearchProvider:
    """WSA API-key endpoint. Both passage and content are search summaries."""

    name = "yuanbao"
    capabilities = {"freshness", "publish_time", "domain_filter"}
    # The chain owns retry accounting; every HTTP attempt consumes one reservation.
    endpoint = "https://api.wsa.cloud.tencent.com/SearchPro"

    def __init__(
        self, api_key: str, *, client: httpx.AsyncClient | None = None, version: str = "standard"
    ):
        if not api_key:
            raise ValueError("元宝搜索 API key 未配置")
        if version not in {"lite", "standard", "flagship"}:
            raise ValueError("元宝搜索版本必须为 lite、standard 或 flagship")
        self.api_key = api_key
        self.version = version
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))

    async def search(self, params: SearchParams) -> list[SearchResult]:
        payload: dict[str, object] = {"Query": params.query}
        if self.version != "lite":
            payload.update(Mode=0, Cnt=min(50, ((params.top_k + 9) // 10) * 10))
        # WSA accepts one inclusive site only. Multiple allowed sites remain a
        # union at the local guard instead of silently searching only its first.
        if len(params.include_domains) == 1:
            payload["Site"] = params.include_domains[0]
        elif not params.include_domains and params.exclude_domains:
            payload["Site"] = "exclude:" + "|".join(params.exclude_domains[:5])
        relative = {"oneDay": "d1", "oneWeek": "d7", "oneMonth": "m1", "oneYear": "y1"}
        if params.freshness in relative:
            payload["Freshness"] = relative[params.freshness]
        elif params.freshness != "noLimit":
            # WSA documents ascending examples but contradictory range prose.
            # Do not guess a wire ordering; keep exact dates for downstream page
            # date verification. Single dates have an unambiguous contract.
            if ".." not in params.freshness:
                payload["Freshness"] = params.freshness
        response = await self.client.post(
            self.endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        response.raise_for_status()
        body = response.json()
        envelope = body.get("Response") if isinstance(body, dict) else None
        if not isinstance(envelope, dict):
            raise ValueError("元宝搜索响应缺少 Response")
        if envelope.get("Error"):
            # Do not surface upstream free text, which may contain credentials.
            raise RuntimeError("元宝搜索返回业务错误，请检查服务版本、授权或额度")
        pages = envelope.get("Pages")
        if not isinstance(pages, list):
            raise ValueError("元宝搜索响应缺少 Pages 数组")
        metadata = {key: envelope[key] for key in ("RequestId", "Version") if key in envelope}
        if ".." in params.freshness:
            metadata["exact_date_filter"] = "local_page_verification"
        results = []
        for encoded in pages:
            if not isinstance(encoded, str):
                raise ValueError("元宝搜索 Pages 项必须是 JSON 字符串")
            page = json.loads(encoded)
            if not isinstance(page, dict):
                raise ValueError("元宝搜索网页结构不合格")
            if page.get("vr"):
                continue
            url, title = page.get("url"), page.get("title")
            snippet = page.get("content") or page.get("passage")
            if not all(isinstance(value, str) and value.strip() for value in (url, title, snippet)):
                continue
            parts = urlsplit(url)
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username:
                continue
            raw = {**page, "date_provenance": "search_provider"}
            date_value = page.get("date")
            date_page = page
            if isinstance(date_value, str) and re.fullmatch(
                r"\d{4}/\d{2}/\d{2}(?: \d{2}:\d{2}:\d{2})?", date_value
            ):
                date_page = {**page, "date": date_value.replace("/", "-")}
            result = SearchResult(
                url=url,
                title=title,
                snippet=snippet,
                source_name=page.get("site") if isinstance(page.get("site"), str) else None,
                published_at=_published_at_value(date_page),
                provider=self.name,
                lang=params.lang,
                provider_metadata=metadata,
                raw=raw,
            )
            results.append(result)
        # Enforce all domains even when called outside SearchChain (discovery,
        # probes, connection tests). Site does not constrain vertical cards.
        from yuqing.core.search.chain import SearchChain

        return SearchChain._apply_domain_filters(results, params)
