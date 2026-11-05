import pytest

from yuqing.core.search.base import SearchResult
from yuqing.services.search_planning import SearchPlanning


class Gateway:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_query_translation_is_real_target_language_and_cached():
    gateway = Gateway({"query": "Wuhan University library investigation", "language": "en"})
    planning = SearchPlanning(gateway)
    for _ in range(2):
        assert (
            await planning.translate("武汉大学图书馆调查", "en")
            == "Wuhan University library investigation"
        )
    assert gateway.calls == 1
    assert (
        await planning.translate("Wuhan University library investigation", "en")
        == "Wuhan University library investigation"
    )
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_wrong_language_or_budget_failure_is_not_relabelled_as_translation():
    planning = SearchPlanning(Gateway({"query": "武汉大学最新报道", "language": "en"}))
    assert await planning.translate("武汉大学事件", "en") is None
    assert await SearchPlanning().translate("武汉大学事件", "en") is None


@pytest.mark.asyncio
async def test_alias_requires_independent_subject_event_match_and_literal_material():
    result = SearchResult(
        url="https://example.org/a",
        title="Wuhan University library complaint",
        snippet="Officials investigate the library incident.",
        provider="fixture",
        lang="en",
    )
    gateway = Gateway(
        {
            "same_subject": True,
            "same_event_context": True,
            "confidence": "high",
            "alias": "Wuhan University",
        }
    )
    planning = SearchPlanning(gateway)
    assert await planning.subject_alias("武汉大学图书馆事件", result) == "Wuhan University"
    gateway.result["alias"] = "Hubei University"
    other = result.model_copy(update={"url": "https://example.org/b"})
    assert await planning.subject_alias("武汉大学图书馆事件", other) is None
    gateway.result.update(alias="Wuhan University", same_subject=False)
    assert await SearchPlanning(gateway).subject_alias("其他大学", result) is None
