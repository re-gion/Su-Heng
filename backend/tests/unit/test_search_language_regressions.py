from datetime import date

import pytest

from yuqing.core.search.base import SearchResult
from yuqing.services.topic_discovery import (
    OpenAITopicQueryPlanner,
    TopicDiscovery,
    TopicDiscoveryRequest,
    _event_terms,
)


class Gateway:
    async def complete_json(self, *args, **kwargs):
        return {
            "queries": [
                "Wuhan University library complaint investigation",
                "Wuhan University latest news",
            ]
        }


@pytest.mark.asyncio
async def test_english_planner_keeps_specific_query_and_rejects_generic_query():
    values = await OpenAITopicQueryPlanner(Gateway()).propose(
        "Wuhan University",
        date_from="2025-01-01",
        date_to="2026-11-05",
        language="en",
        limit=3,
    )
    assert values == ["Wuhan University library complaint investigation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("languages", [("zh", "en"), ("en", "zh")])
@pytest.mark.parametrize("phase", ["initial", "recovery", "manual"])
async def test_discovery_queries_cover_both_languages(languages, phase):
    discovery = TopicDiscovery(object(), object(), today=lambda: date(2026, 11, 5))
    request = TopicDiscoveryRequest(
        topic="CrowdStrike",
        languages=languages,
        manual_event_query="CrowdStrike outage" if phase == "manual" else None,
    )
    effective, _ = discovery._effective_range(request)
    queries = await discovery._queries(
        request,
        effective,
        recovery=phase == "recovery",
        recovery_hints=["CrowdStrike update outage", "CrowdStrike lawsuit"],
    )
    assert {lang for lang, _ in queries} == set(languages)
    assert [lang for lang, _ in queries[:2]] == list(languages)
    for lang, query in queries:
        if lang == "en":
            assert "通报" not in query and "调查" not in query


def test_english_event_passes_event_guard_and_has_cluster_terms():
    discovery = TopicDiscovery(object(), object())
    request = TopicDiscoveryRequest(topic="Wuhan University", languages=("en",))
    effective, _ = discovery._effective_range(request)
    item = SearchResult(
        url="https://example.org/news",
        title="Wuhan University investigates library complaint",
        snippet="Officials respond to library incident allegations.",
        provider="fixture",
        lang="en",
    )
    assert discovery._reject_reason(item, request, effective) is None
    assert "library" in _event_terms(item.title, request.topic)


def test_english_clusters_require_specific_shared_event_objects():
    from types import SimpleNamespace

    from yuqing.services.topic_discovery import _same_event

    topic = "Wuhan University"
    first = SimpleNamespace(
        event_terms=_event_terms(
            "Wuhan University investigates student library complaint official response", topic
        )
    )
    other = SimpleNamespace(
        event_terms=_event_terms(
            "Wuhan University investigates student laboratory fire official response", topic
        )
    )
    same = SimpleNamespace(
        event_terms=_event_terms(
            "Wuhan University responds to library complaint allegations", topic
        )
    )
    assert not _same_event(first, other)
    assert _same_event(first, same)


def test_chinese_material_can_match_an_english_event_with_confirmed_alias():
    from yuqing.services.investigation_scope import InvestigationScope

    scope = InvestigationScope(
        event_query="Wuhan University library complaint",
        languages=("zh",),
        subject_aliases=("武汉大学",),
        source_scope="global",
    )
    item = SearchResult(
        url="https://example.cn/news",
        title="武汉大学回应图书馆投诉调查",
        snippet="校方发布调查通报。",
        provider="fixture",
        lang="zh",
    )
    assert scope.classify_result(
        item, agent="fact_investigator", search_query="武汉大学 图书馆 投诉 调查"
    ).accepted
    unrelated = item.model_copy(update={"title": "武汉大学校园介绍", "snippet": "校园环境优美。"})
    assert not scope.classify_result(unrelated, agent="fact_investigator").accepted

    other_event = item.model_copy(
        update={
            "title": "武汉大学实验室火灾调查通报",
            "snippet": "校方通报实验室火灾调查处理结果。",
        }
    )
    assert not scope.classify_result(
        other_event, agent="fact_investigator", search_query="武汉大学 图书馆 投诉 调查"
    ).accepted


@pytest.mark.asyncio
async def test_failed_language_does_not_count_as_completed_coverage():
    from datetime import datetime
    from types import SimpleNamespace

    class Provider:
        capabilities = set()

        def __init__(self, name):
            self.name = name

        async def search(self, params):
            if params.lang == "en":
                raise RuntimeError("fixture unavailable")
            title = "Wuhan University 图书馆投诉调查通报"
            return [
                SearchResult(
                    url=f"https://{self.name}.example.cn/event",
                    title=title,
                    snippet=title,
                    content_text=title * 30,
                    content_origin="provider_fulltext",
                    provider=self.name,
                    published_at=datetime(2026, 10, 1),
                )
            ]

    discovery = TopicDiscovery(
        SimpleNamespace(providers=[Provider("first"), Provider("second")]),
        object(),
        today=lambda: date(2026, 11, 5),
    )
    outcome = await discovery.discover(
        TopicDiscoveryRequest(
            topic="Wuhan University", languages=("zh", "en"), max_search_calls=8, max_fetch_calls=0
        )
    )
    assert len(outcome.provider_coverage.successful) == 2
    assert outcome.provider_coverage.limited
    assert "未完成检索的语种：en" in outcome.provider_coverage.message
