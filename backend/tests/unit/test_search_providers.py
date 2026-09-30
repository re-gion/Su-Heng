import json

import httpx
import pytest

from yuqing.core.search.base import SearchParams
from yuqing.core.search.langsearch import LangSearchLimiter, LangSearchProvider
from yuqing.core.search.providers import (
    BochaSearchProvider,
    ExaSearchProvider,
    QianfanSearchProvider,
    SerperSearchProvider,
    TavilySearchProvider,
)


def _client(captured: dict, body: dict, *, status_code: int = 200) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(status_code, json=body, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_langsearch_uses_current_camel_case_filters_and_provider_fulltext():
    captured: dict = {}
    client = _client(
        captured,
        {
            "code": 200,
            "log_id": "ls-1",
            "data": {
                "webPages": {
                    "value": [
                        {
                            "url": "https://example.com/article",
                            "name": "标题",
                            "text": "网页全文",
                            "datePublished": "2026-09-01T00:00:00Z",
                        }
                    ]
                }
            },
            "usage": {"input_tokens": 2, "output_tokens": 8},
        },
    )
    provider = LangSearchProvider(
        "key", client=client, limiter=LangSearchLimiter(0, max_calls_per_minute=10)
    )
    results = await provider.search(
        SearchParams(
            query="事件",
            freshness="2026-09-01..2026-09-22",
            include_domains=["example.com"],
            exclude_domains=["ads.example.com"],
        )
    )

    assert captured["payload"]["contents"] == {"text": {"maxCharacters": 3000}}
    assert captured["payload"]["includeDomains"] == ["example.com"]
    assert captured["payload"]["excludeDomains"] == ["ads.example.com"]
    assert "summary" not in captured["payload"]
    assert results[0].content_text == "网页全文"
    assert results[0].content_origin == "provider_fulltext"
    assert results[0].usage == {"input_tokens": 2, "output_tokens": 8}
    assert results[0].provider_metadata["log_id"] == "ls-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_langsearch_snippet_first_keeps_exa_text_setting_independent():
    captured: dict = {}
    client = _client(
        captured,
        {
            "data": {
                "webPages": {
                    "value": [
                        {
                            "url": "https://example.com/article",
                            "name": "事件标题",
                            "snippet": "事件摘要",
                            "text": "上游意外返回的正文",
                        }
                    ]
                }
            }
        },
    )
    provider = LangSearchProvider(
        "key", client=client, limiter=LangSearchLimiter(0, max_calls_per_minute=10)
    )

    results = await provider.search(
        SearchParams(query="事件", contents_text=True, langsearch_contents_text=False)
    )

    assert captured["payload"]["contents"] == {"text": False}
    assert results[0].snippet == "事件摘要"
    assert results[0].content_text is None
    assert results[0].content_origin == "search_snippet"
    await client.aclose()


@pytest.mark.asyncio
async def test_bocha_parses_web_pages_and_uses_domain_fields():
    captured: dict = {}
    client = _client(
        captured,
        {
            "code": 200,
            "log_id": "bocha-1",
            "data": {
                "webPages": {
                    "value": [
                        {
                            "url": "https://news.example.com/a",
                            "name": "博查标题",
                            "snippet": "短摘要",
                            "summary": "长摘要",
                            "siteName": "示例媒体",
                            "datePublished": "2026-09-02T00:00:00+08:00",
                        }
                    ]
                }
            },
        },
    )
    provider = BochaSearchProvider("key", client=client)
    results = await provider.search(
        SearchParams(query="事件", include_domains=["example.com", "news.example.com"])
    )

    assert captured["url"].endswith("/v1/web-search")
    assert captured["payload"]["include"] == "example.com|news.example.com"
    assert captured["payload"]["summary"] is True
    assert results[0].title == "博查标题"
    assert results[0].summary == "长摘要"
    assert results[0].content_text is None
    assert results[0].content_origin == "search_snippet"
    await client.aclose()


@pytest.mark.asyncio
async def test_exa_search_requests_text_and_limits_provider_text_locally():
    captured: dict = {}
    client = _client(
        captured,
        {
            "requestId": "exa-1",
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "Exa 标题",
                    "publishedDate": "2026-09-03T00:00:00Z",
                    "text": "全文内容" * 1000,
                }
            ],
            "costDollars": {"total": 0.008},
        },
    )
    provider = ExaSearchProvider("key", client=client)
    results = await provider.search(
        SearchParams(
            query="global event",
            lang="en",
            freshness="2026-09-01..2026-09-22",
            max_characters=256,
        )
    )

    assert captured["url"] == "https://api.exa.ai/search"
    assert captured["headers"]["x-api-key"] == "key"
    assert captured["payload"]["contents"] == {"text": {"maxCharacters": 256}}
    assert captured["payload"]["startPublishedDate"] == "2026-09-01"
    assert captured["payload"]["endPublishedDate"] == "2026-09-22"
    assert len(results[0].content_text or "") == 256
    assert results[0].content_origin == "provider_fulltext"
    assert results[0].provider_metadata["requestId"] == "exa-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_qianfan_uses_current_messages_and_search_filter_contract():
    captured: dict = {}
    client = _client(
        captured,
        {
            "request_id": "qf-1",
            "references": [
                {
                    "url": "https://example.cn/a",
                    "title": "千帆标题",
                    "content": "千帆摘要",
                    "date": "2026-09-01 12:00:00",
                    "website": "示例站点",
                }
            ],
        },
    )
    provider = QianfanSearchProvider("key", client=client)
    results = await provider.search(
        SearchParams(
            query="事件",
            top_k=5,
            freshness="2026-09-01..2026-09-22",
            include_domains=["example.cn"],
            exclude_domains=["ads.example.cn"],
        )
    )

    payload = captured["payload"]
    assert payload["messages"] == [{"role": "user", "content": "事件"}]
    assert payload["edition"] == "standard"
    assert payload["resource_type_filter"] == [{"type": "web", "top_k": 5}]
    assert payload["search_filter"]["match"]["site"] == ["example.cn"]
    assert payload["search_filter"]["block_websites"] == ["ads.example.cn"]
    assert payload["search_filter"]["range"]["page_time"] == {
        "gte": "2026-09-01",
        "lte": "2026-09-22",
    }
    assert results[0].snippet == "千帆摘要"
    assert results[0].provider_metadata["request_id"] == "qf-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_tavily_uses_time_range_not_legacy_days_field():
    captured: dict = {}
    client = _client(captured, {"results": []})
    provider = TavilySearchProvider("key", client=client)
    await provider.search(SearchParams(query="event", freshness="oneWeek"))

    assert captured["payload"]["time_range"] == "week"
    assert "days" not in captured["payload"]
    await client.aclose()


@pytest.mark.asyncio
async def test_serper_drops_relative_published_time_without_rejecting_results():
    captured: dict = {}
    client = _client(
        captured,
        {
            "news": [
                {
                    "link": "https://example.com/recent",
                    "title": "刚刚发布的报道",
                    "snippet": "Serper 会把相对时间放在 date 字段中。",
                    "date": "5小时前",
                    "source": "示例媒体",
                },
                {
                    "link": "https://example.com/dated",
                    "title": "带绝对日期的报道",
                    "snippet": "绝对日期仍应保留。",
                    "date": "2026-09-22T08:30:00+08:00",
                    "source": "示例媒体",
                },
            ]
        },
    )
    provider = SerperSearchProvider("key", client=client)

    results = await provider.search(SearchParams(query="事件", freshness="oneDay"))

    assert len(results) == 2
    assert results[0].published_at is None
    assert results[0].raw["date"] == "5小时前"
    assert results[1].published_at.isoformat() == "2026-09-22T08:30:00+08:00"
    await client.aclose()
