import json

import httpx
import pytest

from yuqing.core.search.base import SearchParams, SearchResult
from yuqing.core.search.chain import SearchChain
from yuqing.core.search.langsearch import LangSearchLimiter, LangSearchProvider
from yuqing.services.investigation_scope import InvestigationScope

QUERY = "武汉大学图书馆 事件 校方通报 处分决定"
WINDOW = "2023-01-01..2026-01-01"


class Fallback:
    name = "exa"
    capabilities = {"freshness"}

    def __init__(self):
        self.calls = []

    async def search(self, params):
        self.calls.append(params)
        return [
            SearchResult(
                url="https://www.news.cn/20250920/example.htm",
                title="武汉大学通报图书馆事件",
                snippet="武汉大学通报图书馆事件调查复核结果。",
                provider=self.name,
            )
        ]


def provider_client(calls, *, error_on_recovery=False):
    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["freshness"] != "noLimit":
            items = [
                {
                    "url": f"https://example.org/other-university/{i}",
                    "name": "其他大学处分决定",
                    "snippet": "另一所大学公布校园抗议处理结果。",
                }
                for i in range(8)
            ]
        else:
            if error_on_recovery:
                return httpx.Response(502, json={"msg": "upstream unavailable"})
            # Live probe found a 2025 report indexed with a 2026 date.
            items = [
                {
                    "url": "https://www.zaobao.com.sg/realtime/china/story20250920-7542952",
                    "name": "武汉大学通报图书馆事件：撤销记过处分",
                    "snippet": "武汉大学发布图书馆事件调查通报。",
                    "datePublished": "2026-08-18T05:52:03Z",
                }
            ]
        return httpx.Response(200, json={"data": {"webPages": {"value": items}}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def predicate(source_scope="global"):
    scope = InvestigationScope(
        event_query="武汉大学图书馆事件",
        languages=("zh",),
        source_scope=source_scope,
        date_from="2023-01-01",
        date_to="2026-01-01",
    )

    def accept(item):
        decision = scope.classify_result(item, agent="fact_investigator", search_query=QUERY)
        return decision.accepted, decision.reasons

    return accept


@pytest.mark.asyncio
async def test_dated_langsearch_recovers_before_spending_exa():
    calls = []
    async with provider_client(calls) as client:
        provider = LangSearchProvider("test", client=client, limiter=LangSearchLimiter(0))
        exa = Fallback()
        chain = SearchChain([provider, exa])
        reservations = []

        async def reserve():
            reservations.append(True)
            return True

        results = await chain.search_filtered(
            SearchParams(query=QUERY, freshness=WINDOW, allow_freshness_fallback=True),
            predicate(),
            before_call=reserve,
        )
        assert len(calls) == 2
        assert [p["freshness"] for p in calls] == [WINDOW, "noLimit"]
        assert len(reservations) == 2
        assert not exa.calls
        assert [r.provider for r in results] == ["langsearch"]
        assert results[0].published_at.year == 2026  # Never invent a corrected date.
        assert chain.last_diagnostics[0]["rejected"] == {"subject_mismatch": 8}
        assert chain.last_diagnostics[1]["recovery_reason"] == "relax_preferred_date_window"
        assert chain.last_degraded_from is None


@pytest.mark.asyncio
async def test_recovery_preserves_domestic_gate_and_original_exa_window():
    calls = []
    async with provider_client(calls) as client:
        chain = SearchChain(
            [LangSearchProvider("test", client=client, limiter=LangSearchLimiter(0)), Fallback()]
        )
        results = await chain.search_filtered(
            SearchParams(query=QUERY, freshness=WINDOW, allow_freshness_fallback=True),
            predicate("domestic"),
        )
        assert len(calls) == 2
        assert [r.provider for r in results] == ["exa"]
        assert chain.providers[1].calls[0].freshness == WINDOW
        assert chain.last_diagnostics[1]["rejected"] == {"foreign_source_in_domestic_phase": 1}
        assert chain.last_diagnostics[0]["rejected"] == {"subject_mismatch": 8}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("freshness", "opt_in"), [(WINDOW, False), ("oneWeek", True), ("noLimit", True)]
)
async def test_strict_recent_and_unbounded_searches_are_not_relaxed(freshness, opt_in):
    calls = []
    async with provider_client(calls) as client:
        chain = SearchChain(
            [LangSearchProvider("test", client=client, limiter=LangSearchLimiter(0)), Fallback()]
        )
        await chain.search_filtered(
            SearchParams(query=QUERY, freshness=freshness, allow_freshness_fallback=opt_in),
            lambda item: False,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_recovery_cannot_bypass_call_budget():
    calls = []
    reservations = 0

    async def reserve():
        nonlocal reservations
        reservations += 1
        return reservations <= 1

    async with provider_client(calls) as client:
        exa = Fallback()
        chain = SearchChain(
            [LangSearchProvider("test", client=client, limiter=LangSearchLimiter(0)), exa]
        )
        results = await chain.search_filtered(
            SearchParams(query=QUERY, freshness=WINDOW, allow_freshness_fallback=True),
            lambda item: False,
            before_call=reserve,
        )
        assert results == []
        assert len(calls) == 1
        assert not exa.calls
        assert chain.last_diagnostics[-1]["status"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_failed_recovery_still_uses_exa():
    calls = []
    async with provider_client(calls, error_on_recovery=True) as client:
        chain = SearchChain(
            [LangSearchProvider("test", client=client, limiter=LangSearchLimiter(0)), Fallback()]
        )
        results = await chain.search_filtered(
            SearchParams(query=QUERY, freshness=WINDOW, allow_freshness_fallback=True),
            predicate(),
        )
        assert len(calls) == 2
        assert results[0].provider == "exa"
        assert chain.last_diagnostics[1]["status"] == "error"


@pytest.mark.asyncio
async def test_domain_restrictions_survive_recovery():
    calls = []
    async with provider_client(calls) as client:
        chain = SearchChain(
            [LangSearchProvider("test", client=client, limiter=LangSearchLimiter(0)), Fallback()]
        )
        results = await chain.search_filtered(
            SearchParams(
                query=QUERY,
                freshness=WINDOW,
                allow_freshness_fallback=True,
                include_domains=["news.cn"],
                exclude_domains=["blocked.news.cn"],
            ),
            lambda item: True,
        )
        assert len(calls) == 2
        assert all(p["includeDomains"] == ["news.cn"] for p in calls)
        assert all(p["excludeDomains"] == ["blocked.news.cn"] for p in calls)
        assert results[0].provider == "exa"
