from __future__ import annotations

from collections import defaultdict

import pytest

from yuqing.core.search.base import SearchParams, SearchResult
from yuqing.services.provider_quota import (
    ProviderQuotaExceeded,
    ProviderQuotaManager,
    ProviderRateLimiter,
    QuotaAwareFetchProvider,
    QuotaAwareSearchProvider,
)


class MemoryQuotaDatabase:
    def __init__(self):
        self.used: dict[tuple[str, str], int] = defaultdict(int)
        self.last_entries = []

    async def consume_quota_bundle(self, entries):
        self.last_entries = entries
        if any(
            self.used[(scope, period)] + amount > limit for scope, period, limit, amount in entries
        ):
            return False
        for scope, period, _limit, amount in entries:
            self.used[(scope, period)] += amount
        return True

    async def record_provider_usage(self, scope, period, amount):
        self.used[(scope, period)] += amount

    async def provider_usage(self, scope, period):
        return self.used[(scope, period)]


class StaticProvider:
    name = "langsearch"
    capabilities = {"freshness"}
    retry_managed = True

    def __init__(self):
        self.last_usage = {"input_tokens": 4, "output_tokens": 96}

    async def search(self, params):
        return [
            SearchResult(
                url="https://example.com/a",
                title="A",
                snippet="摘要",
                provider=self.name,
                lang=params.lang,
            )
        ]


class StaticFetcher:
    async def fetch(self, url):
        return type("Result", (), {"url": url, "content_text": "正文"})()


@pytest.mark.anyio
async def test_qianfan_regular_pool_preserves_daily_emergency_reserve():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]
    params = SearchParams(query="测试")

    for _ in range(40):
        await manager.reserve("qianfan", params)
    with pytest.raises(ProviderQuotaExceeded):
        await manager.reserve("qianfan", params)

    for _ in range(10):
        await manager.reserve("qianfan", params, critical=True)
    with pytest.raises(ProviderQuotaExceeded):
        await manager.reserve("qianfan", params, critical=True)


@pytest.mark.anyio
async def test_bocha_regular_pool_uses_80_percent_of_one_time_trial():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]
    params = SearchParams(query="测试")

    for _ in range(800):
        await manager.reserve("bocha", params)
    with pytest.raises(ProviderQuotaExceeded):
        await manager.reserve("bocha", params)

    status = await manager.public_status()
    assert status["bocha"]["windows"][0]["normal_limit"] == 800
    assert status["bocha"]["windows"][0]["critical_remaining"] == 200


@pytest.mark.anyio
async def test_exa_reserves_search_and_requested_text_pages_in_milli_dollars():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]

    await manager.reserve("exa", SearchParams(query="test", top_k=5, lang="en"))

    status = (await manager.public_status())["exa"]
    assert database.last_entries == []
    assert status["windows"][0]["used"] == 12
    assert status["activity"] == {"day_calls": 1, "month_calls": 1}


@pytest.mark.anyio
async def test_exa_keeps_old_monthly_usage_without_blocking_normal_or_critical_search():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]
    month = (await manager.public_status())["exa"]["windows"][0]["period_key"]
    database.used[("search:exa:milli_usd", month)] = 20000

    await manager.reserve("exa", SearchParams(query="test", top_k=5))
    await manager.reserve("exa", SearchParams(query="test", top_k=5), critical=True)

    status = (await manager.public_status())["exa"]
    assert status["state"] == "metered_without_fixed_limit"
    assert status["upstream_quota_verified"] is False
    window = status["windows"][0]
    assert window["used"] == 20024
    for field in ("normal_limit", "critical_limit", "normal_remaining", "critical_remaining"):
        assert window[field] is None


@pytest.mark.anyio
async def test_exa_task_guard_does_not_record_cost_for_rejected_search():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]
    params = SearchParams(query="test", top_k=5)
    await manager.reserve("exa", params, task_id="task", task_limit=1)
    with pytest.raises(ProviderQuotaExceeded):
        await manager.reserve("exa", params, task_id="task", task_limit=1)
    status = (await manager.public_status())["exa"]
    assert status["windows"][0]["used"] == 12
    assert status["activity"]["month_calls"] == 1


@pytest.mark.anyio
async def test_langsearch_records_actual_tokens_without_inventing_public_tpd_limit():
    database = MemoryQuotaDatabase()
    provider = QuotaAwareSearchProvider(
        StaticProvider(),
        ProviderQuotaManager(database),  # type: ignore[arg-type]
    )

    results = await provider.search(SearchParams(query="测试"))

    assert results
    assert (
        database.used[
            (
                "search:langsearch:tokens",
                next(
                    key
                    for scope, key in database.used
                    if scope == "search:langsearch:tokens" and key.startswith("day:")
                ),
            )
        ]
        == 100
    )
    status = await ProviderQuotaManager(database).public_status()  # type: ignore[arg-type]
    assert status["langsearch"]["activity"] == {"day_calls": 1, "month_calls": 1}


@pytest.mark.anyio
async def test_firecrawl_wrapper_reserves_one_monthly_credit_per_selected_page():
    database = MemoryQuotaDatabase()
    provider = QuotaAwareFetchProvider(
        StaticFetcher(),
        ProviderQuotaManager(database),  # type: ignore[arg-type]
    )

    result = await provider.fetch("https://example.com/a")

    assert result.content_text == "正文"
    assert database.last_entries[0][0] == "fetch:firecrawl:credits"
    assert database.last_entries[0][2:] == (800, 1)

    critical = QuotaAwareFetchProvider(
        StaticFetcher(),
        ProviderQuotaManager(database),  # type: ignore[arg-type]
        critical=True,
    )
    await critical.fetch("https://example.com/key-evidence")
    assert database.last_entries[0][2:] == (1000, 1)


@pytest.mark.anyio
async def test_search_wrapper_uses_shared_provider_rate_limiter():
    database = MemoryQuotaDatabase()

    class CountingLimiter(ProviderRateLimiter):
        def __init__(self):
            super().__init__(0)
            self.calls = 0

        async def acquire(self):
            self.calls += 1

    limiter = CountingLimiter()
    provider = QuotaAwareSearchProvider(
        StaticProvider(),
        ProviderQuotaManager(database),  # type: ignore[arg-type]
        limiter=limiter,
    )

    await provider.search(SearchParams(query="测试"))

    assert limiter.calls == 1


@pytest.mark.anyio
async def test_limited_provider_task_cap_spans_discovery_and_investigation_calls():
    database = MemoryQuotaDatabase()
    raw = StaticProvider()
    raw.name = "qianfan"
    provider = QuotaAwareSearchProvider(
        raw,
        ProviderQuotaManager(database),  # type: ignore[arg-type]
        max_calls_per_task=2,
    )
    params = SearchParams(query="具体事件")

    await provider.search(params)  # 主题发现
    await provider.search(params)  # 调查检索
    with pytest.raises(ProviderQuotaExceeded):
        await provider.search(params)

    assert provider.task_limit_reached
    assert (
        next(
            used
            for (scope, period), used in database.used.items()
            if scope == "search:qianfan:calls" and period.startswith("day:")
        )
        == 2
    )

    resumed = QuotaAwareSearchProvider(
        raw,
        ProviderQuotaManager(database),  # type: ignore[arg-type]
        max_calls_per_task=2,
    )
    await provider.bind_task("task-a")
    await resumed.bind_task("task-a")
    assert resumed.task_limit_reached is False  # earlier calls lacked task binding
    await resumed.search(params)
    restarted = QuotaAwareSearchProvider(
        raw,
        ProviderQuotaManager(database),  # type: ignore[arg-type]
        max_calls_per_task=2,
    )
    await restarted.bind_task("task-a")
    assert restarted.task_limit_reached is False
    await restarted.search(params)
    await restarted.bind_task("task-a")
    assert restarted.task_limit_reached is True
    with pytest.raises(ProviderQuotaExceeded):
        await restarted.search(params)


@pytest.mark.anyio
async def test_public_status_exposes_local_limits_without_claiming_upstream_quota():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]
    params = SearchParams(query="测试")
    for _ in range(40):
        await manager.reserve("qianfan", params)

    status = await manager.public_status()

    assert status["qianfan"]["state"] == "normal_limit_reached"
    assert status["qianfan"]["upstream_quota_verified"] is False
    assert status["qianfan"]["windows"][0]["used"] == 40
    assert status["qianfan"]["windows"][0]["normal_limit"] == 40
    assert status["qianfan"]["activity"] == {"day_calls": 40, "month_calls": 40}
    assert status["langsearch"]["state"] == "metered_without_fixed_limit"


@pytest.mark.anyio
async def test_firecrawl_status_shows_monthly_limit_and_local_daily_usage():
    database = MemoryQuotaDatabase()
    manager = ProviderQuotaManager(database)  # type: ignore[arg-type]
    await manager.reserve_fetch("firecrawl")
    status = await manager.fetch_status()
    assert status["windows"][0]["used"] == 1
    assert status["activity"] == {"day_calls": 1, "month_calls": 1}
