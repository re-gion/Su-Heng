import json

import httpx
import pytest

from yuqing.core.search.base import SearchParams
from yuqing.core.search.yuanbao import YuanbaoSearchProvider


@pytest.mark.asyncio
async def test_yuanbao_returns_web_snippets_and_filters_vertical_cards():
    captured = {}

    async def handler(request):
        captured.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-key"
        pages = [
            {
                "url": "https://news.example.org/a",
                "title": "公开通报",
                "passage": "标准摘要",
                "content": "动态摘要",
                "date": "2026/10/30 10:00:00",
                "site": "新闻站",
                "score": 0.9,
            },
            {"vr": True, "url": "https://weather.example.org", "title": "天气", "content": "卡片"},
            {"url": "javascript:alert(1)", "title": "无效链接", "passage": "无效"},
        ]
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Pages": [json.dumps(p, ensure_ascii=False) for p in pages],
                    "Version": "standard",
                    "RequestId": "request-1",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = YuanbaoSearchProvider("test-key", client=client)
        results = await provider.search(SearchParams(query="公开事件", top_k=5))
    assert captured["Mode"] == 0
    assert captured["Cnt"] == 10
    assert len(results) == 1
    assert results[0].snippet == "动态摘要"
    assert results[0].content_text is None
    assert results[0].content_origin == "search_snippet"
    assert results[0].source_name == "新闻站"
    assert results[0].raw["date_provenance"] == "search_provider"
    assert results[0].published_at.isoformat() == "2026-10-30T10:00:00"
    assert results[0].raw["date"] == "2026/10/30 10:00:00"


@pytest.mark.asyncio
async def test_yuanbao_lite_omits_unsupported_options_and_keeps_local_domains():
    captured = {}

    async def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"Response": {"Pages": [], "Version": "lite"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = YuanbaoSearchProvider("test-key", client=client, version="lite")
        await provider.search(
            SearchParams(query="事件", include_domains=["a.cn", "b.cn"], freshness="oneWeek")
        )
    assert not ({"Mode", "Cnt", "Industry", "Site"} & captured.keys())
    assert captured["Freshness"] == "d7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Response": {}},
        {"Response": {"Pages": "invalid"}},
        {"Response": {"Error": {"Code": "AuthFailure", "Message": "sensitive detail"}}},
        {"Response": {"Pages": ["broken-json"]}},
    ],
)
async def test_yuanbao_invalid_or_error_response_is_not_successful_empty(body):
    async def handler(request):
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = YuanbaoSearchProvider("test-key", client=client)
        with pytest.raises((ValueError, RuntimeError)) as caught:
            await provider.search(SearchParams(query="事件"))
    assert "sensitive detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_yuanbao_exclusion_uses_official_separator_and_hard_filters_locally():
    captured = {}

    async def handler(request):
        captured.update(json.loads(request.content))
        pages = [
            {"url": "https://ads.example.cn/a", "title": "事件", "passage": "摘要"},
            {"url": "https://news.example.cn/a", "title": "事件", "passage": "摘要"},
        ]
        return httpx.Response(200, json={"Response": {"Pages": [json.dumps(p) for p in pages]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await YuanbaoSearchProvider("test-key", client=client).search(
            SearchParams(query="事件", exclude_domains=["ads.example.cn", "b.cn"])
        )
    assert captured["Site"] == "exclude:ads.example.cn|b.cn"
    assert [r.url for r in results] == ["https://news.example.cn/a"]
