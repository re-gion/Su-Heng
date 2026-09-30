import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from yuqing.core.search.base import SearchParams, SearchResult
from yuqing.core.search.chain import SearchChain, SearchChainExhausted
from yuqing.core.search.langsearch import LangSearchLimiter, LangSearchProvider
from yuqing.services.investigation_scope import InvestigationScope


class Provider:
    capabilities = {"freshness"}

    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    async def search(self, params):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def post(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def test_chinese_search_uses_exa_before_limited_domestic_backups():
    chain = SearchChain(
        [
            Provider(name, result=[])
            for name in ("langsearch", "exa", "qianfan", "bocha", "tavily", "serper")
        ]
    )

    assert [
        provider.name
        for provider in chain.providers_for(SearchParams(query="武汉大学图书馆事件", lang="zh"))
    ] == ["langsearch", "exa", "qianfan", "bocha"]
    assert [
        provider.name
        for provider in chain.providers_for(SearchParams(query="campus case", lang="en"))
    ] == ["exa", "tavily", "serper"]


@pytest.mark.asyncio
async def test_langsearch_limiter_enforces_minute_window(monkeypatch):
    now = 100.0
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr("yuqing.core.search.langsearch.time.monotonic", lambda: now)
    monkeypatch.setattr("yuqing.core.search.langsearch.asyncio.sleep", fake_sleep)
    limiter = LangSearchLimiter(0, max_calls_per_minute=2)

    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert sleeps == [pytest.approx(60.0)]


@pytest.mark.asyncio
async def test_langsearch_429_honors_retry_after_and_retries(monkeypatch):
    now = 100.0
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr("yuqing.core.search.langsearch.time.monotonic", lambda: now)
    monkeypatch.setattr("yuqing.core.search.langsearch.asyncio.sleep", fake_sleep)
    request = httpx.Request("POST", "https://api.langsearch.com/v1/web-search")
    client = SequenceClient(
        [
            httpx.Response(429, headers={"Retry-After": "2"}, request=request),
            httpx.Response(
                200,
                json={"data": {"webPages": {"value": []}}},
                request=request,
            ),
        ]
    )
    provider = LangSearchProvider(
        "secret",
        client=client,
        limiter=LangSearchLimiter(0, max_calls_per_minute=10),
    )

    assert await provider.search(SearchParams(query="限流恢复")) == []
    assert client.calls == 2
    assert sleeps == [pytest.approx(2.0)]


def test_search_result_repairs_lone_surrogates_from_provider_payload():
    result = SearchResult(
        url="https://example.com/report",
        title="传播标题\ud83d",
        snippet="传播摘要\udc00",
        provider="fixture",
        raw={"nested": ["保留合法字符😀", "替换非法字符\ud83d"]},
    )

    assert json.dumps(result.model_dump(), ensure_ascii=False).encode("utf-8")
    assert result.title == "传播标题�"
    assert result.snippet == "传播摘要�"
    assert result.raw == {"nested": ["保留合法字符😀", "替换非法字符�"]}


@pytest.mark.asyncio
async def test_search_chain_falls_back_and_exposes_degradation():
    primary = Provider("primary", error=RuntimeError("quota"))
    fallback = Provider(
        "fallback",
        result=[
            SearchResult(
                url="https://example.com",
                title="结果",
                snippet="摘要",
                provider="fallback",
            )
        ],
    )
    chain = SearchChain([primary, fallback], failure_threshold=1)

    result = await chain.search(SearchParams(query="测试"))

    assert result[0].provider == "fallback"
    assert chain.last_provider == "fallback"
    assert chain.last_degraded_from == "primary"
    assert chain.last_continued_from == "primary"
    assert chain.statuses()[0]["breaker"] == "open"


@pytest.mark.asyncio
async def test_search_chain_default_breaker_recovers_after_15_seconds(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "yuqing.core.search.chain.time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    primary = Provider("primary", error=RuntimeError("temporary"))
    fallback = Provider("fallback", result=[])
    chain = SearchChain([primary, fallback], failure_threshold=1)

    await chain.search(SearchParams(query="第一次"))
    assert chain.statuses()[0]["breaker"] == "open"

    now[0] = 114.9
    await chain.search(SearchParams(query="等待期间"))
    assert primary.calls == 1
    assert chain.last_diagnostics[0]["reason"] == "breaker_open"

    now[0] = 115.0
    primary.error = None
    primary.result = []
    await chain.search(SearchParams(query="恢复后"))
    assert primary.calls == 2
    assert chain.statuses()[0]["breaker"] == "closed"


@pytest.mark.asyncio
async def test_search_chain_treats_local_quota_guard_as_policy_not_provider_failure():
    class LocalQuotaGuard(RuntimeError):
        kind = "local_quota_guard"

    primary = Provider("primary", error=LocalQuotaGuard("本地常规额度保护线已到达"))
    fallback = Provider(
        "fallback",
        result=[
            SearchResult(
                url="https://example.com",
                title="结果",
                snippet="摘要",
                provider="fallback",
            )
        ],
    )
    chain = SearchChain([primary, fallback], failure_threshold=1)

    result = await chain.search(SearchParams(query="测试"))

    assert result[0].provider == "fallback"
    assert chain.last_degraded_from is None
    assert chain.last_continued_from == "primary"
    assert chain.last_diagnostics[0]["reason"] == "local_quota_guard"
    assert chain.statuses()[0]["breaker"] == "closed"


@pytest.mark.asyncio
async def test_search_chain_enforces_domain_filter_and_tries_next_provider():
    primary = Provider(
        "primary",
        result=[
            SearchResult(
                url="https://en.wikipedia.org/wiki/Unrelated",
                title="站外结果",
                snippet="上游忽略了域名过滤",
                provider="primary",
            )
        ],
    )
    fallback = Provider(
        "fallback",
        result=[
            SearchResult(
                url="https://www.bilibili.com/video/BV1xx411c7mD",
                title="目标平台帖子",
                snippet="与事件相关",
                provider="fallback",
            )
        ],
    )
    chain = SearchChain([primary, fallback])

    results = await chain.search(SearchParams(query="具体事件", include_domains=["bilibili.com"]))

    assert [item.url for item in results] == ["https://www.bilibili.com/video/BV1xx411c7mD"]
    assert primary.calls == fallback.calls == 1
    assert chain.last_provider == "fallback"
    assert chain.last_degraded_from is None
    assert chain.last_continued_from == "primary"


@pytest.mark.asyncio
async def test_search_chain_tries_next_provider_when_caller_rejects_irrelevant_results():
    primary = Provider(
        "primary",
        result=[
            SearchResult(
                url="https://example.cn/unrelated",
                title="同一机构的无关页面",
                snippet="不含具体事件语义",
                provider="primary",
            )
        ],
    )
    fallback = Provider(
        "fallback",
        result=[
            SearchResult(
                url="https://example.cn/relevant",
                title="具体事件调查复核通报",
                snippet="包含具体事件语义",
                provider="fallback",
            )
        ],
    )
    chain = SearchChain([primary, fallback])

    results = await chain.search_filtered(
        SearchParams(query="具体事件"),
        lambda item: "调查复核" in item.title,
    )

    assert [item.provider for item in results] == ["fallback"]
    assert primary.calls == fallback.calls == 1
    assert chain.last_provider == "fallback"
    assert chain.last_degraded_from is None
    assert chain.last_continued_from == "primary"
    assert chain.last_diagnostics[0]["status"] == "relevance_filtered_empty"


@pytest.mark.asyncio
async def test_search_chain_counts_fallback_requests_against_task_budget():
    primary = Provider("langsearch", result=[])
    fallback = Provider("qianfan", result=[])
    chain = SearchChain([primary, fallback])
    reservations = 0

    async def reserve():
        nonlocal reservations
        if reservations >= 1:
            return False
        reservations += 1
        return True

    result = await chain.search_filtered(
        SearchParams(query="具体事件"), lambda _: True, before_call=reserve
    )

    assert result == []
    assert primary.calls == 1
    assert fallback.calls == 0
    assert reservations == 1
    assert chain.last_diagnostics[-1]["status"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_chinese_event_reaches_exa_without_spending_qianfan():
    free = Provider(
        "langsearch",
        result=[
            SearchResult(
                url="https://zh.wikipedia.org/wiki/example-history-dispute",
                title="武汉大学校史争议",
                snippet="武汉大学校方回应称，校舍和图书资料的继承有大量史实。",
                provider="langsearch",
                lang="zh",
            )
        ],
    )
    exa = Provider(
        "exa",
        result=[
            SearchResult(
                url="https://www.news.cn/politics/20250920/report.html",
                title="武大通报图书馆事件调查复核情况",
                snippet="武汉大学通报图书馆事件调查复核情况。",
                provider="exa",
                lang="zh",
            )
        ],
    )
    limited = Provider("qianfan", result=[])
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    chain = SearchChain([free, exa, limited])

    results = await chain.search_filtered(
        SearchParams(query="武汉大学 图书馆事件 调查复核 官方通报", lang="zh"),
        lambda item: scope.classify_result(item, agent="fact_investigator").accepted,
    )

    assert [item.provider for item in results] == ["exa"]
    assert (free.calls, exa.calls, limited.calls) == (1, 1, 0)
    assert chain.last_diagnostics[0]["status"] == "relevance_filtered_empty"


@pytest.mark.asyncio
async def test_langsearch_one_source_is_supplemented_by_exa_without_spending_qianfan():
    langsearch = Provider(
        "langsearch",
        result=[
            SearchResult(
                url=f"https://www.zaobao.com.sg/story-{index}",
                title=f"事件报道 {index}",
                snippet="与事件相关",
                provider="langsearch",
            )
            for index in (1, 2)
        ],
    )
    exa = Provider(
        "exa",
        result=[
            SearchResult(
                url="https://www.zaobao.com.sg/story-1",
                title="重复报道",
                snippet="与事件相关",
                provider="exa",
            ),
            SearchResult(
                url="https://www.news.cn/event-report",
                title="另一来源的报道",
                snippet="与事件相关",
                provider="exa",
            ),
        ],
    )
    qianfan = Provider("qianfan", result=[])
    chain = SearchChain([langsearch, exa, qianfan])

    results = await chain.search_filtered(
        SearchParams(query="具体事件", top_k=2, lang="zh"),
        lambda item: True,
        min_source_groups=2,
        source_group=lambda item: (urlsplit(item.url).hostname or "").removeprefix("www."),
    )

    assert [item.url for item in results] == [
        "https://www.zaobao.com.sg/story-1",
        "https://www.news.cn/event-report",
    ]
    assert (langsearch.calls, exa.calls, qianfan.calls) == (1, 1, 0)
    assert [item["status"] for item in chain.last_diagnostics] == [
        "insufficient_coverage",
        "success",
    ]
    assert chain.last_continued_from == "langsearch"


@pytest.mark.asyncio
async def test_langsearch_diverse_sources_do_not_spend_exa():
    langsearch = Provider(
        "langsearch",
        result=[
            SearchResult(
                url=f"https://{host}/report",
                title="相关报道",
                snippet="与事件相关",
                provider="langsearch",
            )
            for host in ("news.cn", "people.com.cn")
        ],
    )
    exa = Provider("exa", result=[])
    chain = SearchChain([langsearch, exa])

    results = await chain.search_filtered(
        SearchParams(query="具体事件", lang="zh"),
        lambda item: True,
        min_source_groups=2,
        source_group=lambda item: urlsplit(item.url).hostname or "",
    )

    assert len(results) == 2
    assert (langsearch.calls, exa.calls) == (1, 0)


@pytest.mark.asyncio
async def test_exa_budget_exhaustion_preserves_langsearch_lead():
    langsearch = Provider(
        "langsearch",
        result=[
            SearchResult(
                url="https://www.zaobao.com.sg/story-1",
                title="事件报道",
                snippet="与事件相关",
                provider="langsearch",
            )
        ],
    )
    exa = Provider("exa", result=[])
    qianfan = Provider("qianfan", result=[])
    chain = SearchChain([langsearch, exa, qianfan])
    reservations = 0

    async def reserve():
        nonlocal reservations
        reservations += 1
        return reservations <= 1

    results = await chain.search_filtered(
        SearchParams(query="具体事件", lang="zh"),
        lambda item: True,
        before_call=reserve,
        min_source_groups=2,
        source_group=lambda item: urlsplit(item.url).hostname or "",
    )

    assert [item.provider for item in results] == ["langsearch"]
    assert (langsearch.calls, exa.calls, qianfan.calls) == (1, 0, 0)
    assert chain.last_diagnostics[-1]["status"] == "budget_exhausted"
    assert chain.last_provider == "langsearch"


@pytest.mark.asyncio
@pytest.mark.parametrize("exa_error", [False, True])
async def test_exa_without_new_material_tries_limited_api_and_keeps_langsearch(exa_error):
    langsearch = Provider(
        "langsearch",
        result=[
            SearchResult(
                url="https://www.zaobao.com.sg/story-1",
                title="事件报道",
                snippet="与事件相关",
                provider="langsearch",
            )
        ],
    )
    exa = Provider("exa", result=[], error=RuntimeError("temporary") if exa_error else None)
    qianfan = Provider("qianfan", result=[])
    chain = SearchChain([langsearch, exa, qianfan])

    results = await chain.search_filtered(
        SearchParams(query="具体事件", lang="zh"),
        lambda item: True,
        min_source_groups=2,
    )

    assert [item.provider for item in results] == ["langsearch"]
    assert (langsearch.calls, exa.calls, qianfan.calls) == (1, 1, 1)
    assert chain.last_provider == "langsearch"


@pytest.mark.asyncio
async def test_chinese_sparse_sources_use_qianfan_then_stop_before_bocha():
    langsearch = Provider(
        "langsearch",
        result=[
            SearchResult(
                url="https://news.cn/event",
                title="事件报道",
                snippet="与事件相关",
                provider="langsearch",
            )
        ],
    )
    exa = Provider(
        "exa",
        result=[
            SearchResult(
                url="https://news.cn/event",
                title="重复报道",
                snippet="与事件相关",
                provider="exa",
            )
        ],
    )
    qianfan = Provider(
        "qianfan",
        result=[
            SearchResult(
                url="https://people.com.cn/event",
                title="另一发布主体的报道",
                snippet="与事件相关",
                provider="qianfan",
            )
        ],
    )
    bocha = Provider("bocha", result=[])
    chain = SearchChain([langsearch, exa, qianfan, bocha])

    results = await chain.search_filtered(
        SearchParams(query="具体事件", lang="zh"),
        lambda item: True,
        min_source_groups=2,
        source_group=lambda item: urlsplit(item.url).hostname or "",
    )

    assert [item.provider for item in results] == ["langsearch", "qianfan"]
    assert (langsearch.calls, exa.calls, qianfan.calls, bocha.calls) == (1, 1, 1, 0)
    assert [item["status"] for item in chain.last_diagnostics] == [
        "insufficient_coverage",
        "insufficient_coverage",
        "success",
    ]


@pytest.mark.asyncio
async def test_chinese_sparse_sources_reach_bocha_with_task_budget():
    langsearch = Provider("langsearch", result=[])
    exa = Provider(
        "exa",
        result=[
            SearchResult(
                url="https://news.cn/event",
                title="事件报道",
                snippet="与事件相关",
                provider="exa",
            )
        ],
    )
    qianfan = Provider("qianfan", result=[])
    bocha = Provider(
        "bocha",
        result=[
            SearchResult(
                url="https://people.com.cn/event",
                title="另一发布主体的报道",
                snippet="与事件相关",
                provider="bocha",
            )
        ],
    )
    chain = SearchChain([langsearch, exa, qianfan, bocha])
    reservations = 0

    async def reserve():
        nonlocal reservations
        reservations += 1
        return True

    results = await chain.search_filtered(
        SearchParams(query="具体事件", lang="zh"),
        lambda item: True,
        before_call=reserve,
        min_source_groups=2,
        source_group=lambda item: urlsplit(item.url).hostname or "",
    )

    assert [item.provider for item in results] == ["exa", "bocha"]
    assert (langsearch.calls, exa.calls, qianfan.calls, bocha.calls) == (1, 1, 1, 1)
    assert reservations == 4


@pytest.mark.asyncio
async def test_english_sparse_sources_use_tavily_then_serper():
    exa = Provider(
        "exa",
        result=[
            SearchResult(
                url="https://example.org/event",
                title="Event report",
                snippet="Relevant to the event",
                provider="exa",
            )
        ],
    )
    tavily = Provider(
        "tavily",
        result=[
            SearchResult(
                url="https://example.org/another-report",
                title="Another report from the same publisher",
                snippet="Relevant to the event",
                provider="tavily",
            )
        ],
    )
    serper = Provider(
        "serper",
        result=[
            SearchResult(
                url="https://other.org/event",
                title="Independent publisher report",
                snippet="Relevant to the event",
                provider="serper",
            )
        ],
    )
    chain = SearchChain([exa, tavily, serper])

    results = await chain.search_filtered(
        SearchParams(query="specific event", lang="en"),
        lambda item: True,
        min_source_groups=2,
        source_group=lambda item: urlsplit(item.url).hostname or "",
    )

    assert [item.provider for item in results] == ["exa", "serper", "tavily"]
    assert (exa.calls, tavily.calls, serper.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_history_case_from_exa_does_not_fall_through_to_tavily():
    exa = Provider(
        "exa",
        result=[
            SearchResult(
                url="https://example.org/campus-discipline-case",
                title="University publishes findings after student misconduct investigation",
                snippet="A university reviewed student misconduct allegations and published its findings.",
                provider="exa",
                lang="en",
            )
        ],
    )
    tavily = Provider("tavily", result=[])
    scope = InvestigationScope(
        event_query="武汉大学通报图书馆事件调查复核情况",
        languages=("zh",),
        source_scope="domestic",
    )
    query = "university student misconduct investigation outcome case"
    chain = SearchChain([exa, tavily])

    results = await chain.search_filtered(
        SearchParams(query=query, lang="en"),
        lambda item: (
            scope.classify_result(
                item,
                agent="history_insight",
                phase="foreign_supplement",
                search_query=query,
            ).accepted
        ),
    )

    assert [item.provider for item in results] == ["exa"]
    assert (exa.calls, tavily.calls) == (1, 0)


@pytest.mark.asyncio
async def test_search_chain_returns_empty_when_only_provider_ignores_domain_filter():
    provider = Provider(
        "only",
        result=[
            SearchResult(
                url="https://example.com/not-a-post",
                title="站外结果",
                snippet="不能泄漏到调用方",
                provider="only",
            )
        ],
    )

    results = await SearchChain([provider]).search(
        SearchParams(query="事件", include_domains=["weibo.com"])
    )

    assert results == []


@pytest.mark.asyncio
async def test_search_chain_skips_provider_missing_required_capability():
    incapable = Provider("incapable", result=[])
    incapable.capabilities = set()
    capable = Provider("capable", result=[])
    chain = SearchChain([incapable, capable])

    await chain.search(SearchParams(query="历史回溯", freshness="oneYear"))

    assert incapable.calls == 0
    assert capable.calls == 1


@pytest.mark.asyncio
async def test_single_provider_is_not_permanently_skipped_after_transient_failure():
    provider = Provider("only", error=RuntimeError("transient"))
    chain = SearchChain([provider], failure_threshold=1)
    with pytest.raises(SearchChainExhausted):
        await chain.search(SearchParams(query="第一次"))
    provider.error = None
    provider.result = []

    await chain.search(SearchParams(query="第二次"))

    assert provider.calls == 3  # 首次请求重试一次，下一次调用仍可恢复
    assert chain.statuses()[0]["breaker"] == "closed"


@pytest.mark.asyncio
async def test_breaker_state_can_be_shared_across_task_chains():
    failures: dict[str, int] = {}
    opened_at: dict[str, float] = {}
    broken = Provider("shared", error=RuntimeError("quota"))
    first = SearchChain([broken], failure_threshold=1, failures=failures, opened_at=opened_at)
    with pytest.raises(SearchChainExhausted):
        await first.search(SearchParams(query="任务一"))

    # 单 provider 路径允许有界重试但不永久自锁；多 provider 熔断状态另测。
    assert broken.calls == 2

    primary = Provider("primary", error=RuntimeError("quota"))
    fallback = Provider("fallback", result=[])
    chain_a = SearchChain(
        [primary, fallback], failure_threshold=1, failures=failures, opened_at=opened_at
    )
    await chain_a.search(SearchParams(query="任务二"))
    chain_b = SearchChain(
        [Provider("primary", result=[]), Provider("fallback", result=[])],
        failure_threshold=1,
        failures=failures,
        opened_at=opened_at,
    )
    await chain_b.search(SearchParams(query="任务三"))

    assert chain_b.last_provider == "fallback"
