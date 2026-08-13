import pytest

from yuqing.core.search.base import SearchParams, SearchResult
from yuqing.core.search.chain import SearchChain, SearchChainExhausted


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
