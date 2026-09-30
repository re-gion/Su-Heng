import httpx
import pytest

from yuqing.agents.runtime import InvestigationPlan, SearchQuery
from yuqing.core.search.base import SearchParams
from yuqing.core.search.chain import SearchChain
from yuqing.core.search.providers import SerperSearchProvider
from yuqing.services.v1_orchestrator import ensure_requested_languages


class Provider:
    capabilities = {"freshness", "domain_filter"}

    def __init__(self, name):
        self.name = name
        self.calls = []

    async def search(self, params):
        self.calls.append(params)
        return []


@pytest.mark.asyncio
async def test_non_chinese_search_uses_foreign_provider_group():
    domestic = Provider("langsearch")
    global_provider = Provider("tavily")
    chain = SearchChain([domestic, global_provider])

    await chain.search(SearchParams(query="CrowdStrike outage", lang="en"))

    # English searches use the configured foreign group instead of spending
    # domestic quota on a query that the foreign providers cover directly.
    assert len(domestic.calls) == 0
    assert len(global_provider.calls) == 1
    assert [item.name for item in chain.providers] == ["langsearch", "tavily"]


@pytest.mark.asyncio
async def test_serper_uses_language_specific_region_instead_of_fixed_china():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"news": []})

    provider = SerperSearchProvider(
        "test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await provider.search(SearchParams(query="global outage", lang="en"))

    assert captured["gl"] == "us"
    assert captured["hl"] == "en"
    await provider.client.aclose()


@pytest.mark.asyncio
async def test_serper_honors_explicit_structured_region():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"news": []})

    provider = SerperSearchProvider(
        "test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await provider.search(SearchParams(query="Ausfall", lang="de", region="AT"))

    assert captured["gl"] == "at"
    assert captured["hl"] == "de"
    await provider.client.aclose()


def test_missing_explicit_language_is_deterministically_added_to_agent_plan():
    plan = InvestigationPlan(
        queries=[SearchQuery(query="CrowdStrike outage", language="en", region="US")]
    )

    completed = ensure_requested_languages(plan, ["de", "en"], "CrowdStrike global outage")

    assert [item.language for item in completed.queries[:2]] == ["de", "en"]
    assert completed.queries[0].region == "DE"
