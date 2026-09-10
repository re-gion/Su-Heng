import json

import httpx
import pytest

from yuqing.core.search.base import SearchParams, SearchResult
from yuqing.core.search.chain import SearchChain, SearchChainExhausted
from yuqing.core.search.langsearch import LangSearchLimiter, LangSearchProvider


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
    assert chain.statuses()[0]["breaker"] == "open"


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
