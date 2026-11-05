import json

import httpx
import pytest

from yuqing.core.search.base import SearchParams
from yuqing.core.search.yuanbao import YuanbaoSearchProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["standard", "flagship"])
async def test_yuanbao_mixed_mode_returns_web_and_traceable_vertical_snippets(version):
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
        provider = YuanbaoSearchProvider("test-key", client=client, version=version)
        results = await provider.search(SearchParams(query="公开事件", top_k=5))
    assert captured["Mode"] == 2
    assert captured["Cnt"] == 10
    assert len(results) == 2
    assert results[0].snippet == "动态摘要"
    assert results[0].content_text is None
    assert results[0].content_origin == "search_snippet"
    assert results[0].source_name == "新闻站"
    assert results[0].raw["date_provenance"] == "search_provider"
    assert results[0].published_at.isoformat() == "2026-10-30T10:00:00"
    assert results[0].raw["date"] == "2026/10/30 10:00:00"
    assert results[1].url == "https://weather.example.org"
    assert results[1].snippet == "卡片"
    assert results[1].raw["vr"] is True
    assert results[1].content_origin == "search_snippet"
    assert results[1].content_text is None


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


@pytest.mark.asyncio
async def test_mixed_vertical_display_preserves_source_and_enforces_domain_filters():
    pages = [
        {
            "vr": True,
            "display": {
                "url": "https://weather.example.cn/a",
                "title": "天气预警",
                "date": "2026/11/05 09:00:00",
                "site": "气象站",
                "alerts": ["大风预警"],
            },
        },
        {
            "vr": True,
            "display": {"url": "https://other.example.cn/a", "title": "天气", "content": "摘要"},
        },
        {"vr": True, "title": "无来源卡片", "content": "没有可回查URL"},
        {
            "vr": True,
            "display": {"url": "javascript:alert(1)", "title": "无效链接", "content": "摘要"},
        },
    ]

    async def handler(request):
        return httpx.Response(
            200, json={"Response": {"Pages": [json.dumps(page) for page in pages]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await YuanbaoSearchProvider("test-key", client=client).search(
            SearchParams(query="天气预警", include_domains=["weather.example.cn"])
        )
    assert len(results) == 1
    assert results[0].url == "https://weather.example.cn/a"
    assert results[0].title == "天气预警"
    assert results[0].source_name == "气象站"
    assert results[0].published_at.isoformat() == "2026-11-05T09:00:00"
    assert "大风预警" in results[0].snippet
    assert results[0].raw["display"]["date"] == "2026/11/05 09:00:00"
    assert results[0].content_origin == "search_snippet"
    assert results[0].content_text is None
