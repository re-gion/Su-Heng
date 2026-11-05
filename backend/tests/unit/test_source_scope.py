import pytest

from yuqing.services.source_scope import SourceScopeResolver
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate


class Gateway:
    def __init__(self, category, confidence="high"):
        self.category, self.confidence, self.calls = category, confidence, 0

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        return {"category": self.category, "confidence": self.confidence, "reason": "事件地域依据"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "topic,category,expected",
    [
        ("美国列车脱轨事件", "foreign", "global"),
        ("China factory recall investigation", "domestic", "domestic"),
        ("跨境产品召回", "cross_border", "global"),
        ("某企业舆情", "unknown", "global"),
    ],
)
async def test_auto_scope_persists_policy_without_changing_requested_scope(
    tmp_path, topic, category, expected
):
    database = Database(tmp_path / "scope.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query=topic))
        await database.save_checkpoint(task.id, "test:verified", {"phase": "verified"})
        gateway = Gateway(category)
        resolver = SourceScopeResolver(gateway)
        value = await resolver.resolve(task)
        await database.set_source_scope_resolution(task.id, value)
        restored = await database.get_task(task.id)
        assert restored.source_scope == "auto"
        assert restored.effective_source_scope == expected
        assert (await database.latest_checkpoint(task.id))["phase"] == "verified"
        assert await resolver.resolve(restored) == value
        assert gateway.calls == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_explicit_scope_skips_model_and_low_confidence_does_not_narrow(tmp_path):
    database = Database(tmp_path / "scope.db")
    await database.initialize()
    try:
        gateway = Gateway("domestic", "low")
        explicit = await database.create_task(TaskCreate(event_query="事件", source_scope="global"))
        assert (await SourceScopeResolver(gateway).resolve(explicit))["status"] == "explicit"
        assert gateway.calls == 0
        auto = await database.create_task(TaskCreate(event_query="事件"))
        assert (await SourceScopeResolver(gateway).resolve(auto))["effective_scope"] == "global"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_selected_event_reclassifies_once_and_failure_falls_back(tmp_path):
    database = Database(tmp_path / "scope.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(event_query="企业舆情", request_kind="topic_discovery")
        )
        gateway = Gateway("unknown")
        resolver = SourceScopeResolver(gateway)
        await database.set_source_scope_resolution(task.id, await resolver.resolve(task))
        await database.set_resolved_event_query(task.id, "企业跨境召回")
        gateway.category = "cross_border"
        selected = await database.get_task(task.id)
        value = await resolver.resolve(selected)
        assert value["stage"] == "event"
        assert value["category"] == "cross_border"
        assert gateway.calls == 2
        gateway.category = "invalid"
        await database.set_resolved_event_query(task.id, "另一事件")
        fallback = await resolver.resolve(await database.get_task(task.id))
        assert fallback["status"] == "fallback"
        assert fallback["effective_scope"] == "global"
    finally:
        await database.close()
