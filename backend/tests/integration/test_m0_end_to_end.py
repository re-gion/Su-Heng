from datetime import datetime

import pytest

from yuqing.agents.runtime import GeneratedClaim, InvestigationPlan, Reflection
from yuqing.core.events import EventBus
from yuqing.core.search.base import SearchResult
from yuqing.core.search.fixture import FixtureSearchProvider
from yuqing.services.orchestrator import M0Orchestrator
from yuqing.services.verifier import VerificationRelation
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate
from yuqing.storage.snapshots import SnapshotStore


class FixtureAgent:
    async def plan(self, event_query):
        return InvestigationPlan(queries=[event_query, f"{event_query} 官方通报"])

    async def summarize(self, event_query, evidence):
        return [
            GeneratedClaim(
                text=f"{event_query}已有两个独立公开来源报道。",
                evidence_ids=[item.local_id for item in evidence],
            )
        ]

    async def reflect(self, event_query, claims):
        return Reflection(
            new_key_findings=[claims[0].text],
            remaining_gaps=[],
            next_queries=[],
            should_continue=False,
            reason="证据已满足 M0 演示",
        )


class FixtureVerifier:
    model_name = "fixture-verifier"

    async def verify(self, claim, evidence):
        material = evidence.content_text or evidence.snippet or ""
        return VerificationRelation(
            relation="support", reason="摘要明确报道该事件", cited_sentence=material
        )


class AlwaysFailFetcher:
    async def fetch(self, url):
        raise RuntimeError("fixture 模拟原文不可取得")


class MustNotRunVerifier:
    model_name = "must-not-run"

    async def verify(self, claim, evidence):
        raise AssertionError("verified 检查点后不应重复核验")


class TwoRoundAgent:
    def __init__(self):
        self.summary_calls = 0

    async def plan(self, event_query):
        return InvestigationPlan(queries=["首轮检索"])

    async def summarize(self, event_query, evidence):
        self.summary_calls += 1
        return [
            GeneratedClaim(
                text=f"第 {self.summary_calls} 轮新增事实。",
                evidence_ids=[evidence[0].local_id],
            )
        ]

    async def reflect(self, event_query, claims):
        should_continue = self.summary_calls == 1
        return Reflection(
            new_key_findings=[claims[-1].text],
            remaining_gaps=["仍需第二轮"] if should_continue else [],
            next_queries=["第二轮补充检索"] if should_continue else [],
            should_continue=should_continue,
            reason="继续补证" if should_continue else "达到增益终点",
        )


@pytest.mark.asyncio
async def test_offline_fixture_survives_restart_and_finishes_with_clickable_report(runtime_dir):
    database = Database(runtime_dir / "yuqing.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="测试事件", depth="quick"))
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.xinhuanet.com/news",
                title="媒体甲报道",
                snippet="媒体甲确认测试事件已有公开报道。",
                source_name="媒体甲",
                provider="fixture",
                published_at=datetime(2026, 8, 12),
            ),
            SearchResult(
                url="https://www.people.com.cn/news",
                title="媒体乙报道",
                snippet="媒体乙确认测试事件已有公开报道。",
                source_name="媒体乙",
                provider="fixture",
                published_at=datetime(2026, 8, 12),
            ),
        ]
    )
    orchestrator = M0Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=AlwaysFailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agent=FixtureAgent(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
    )
    await orchestrator.run_task(task.id, halt_after_checkpoint="investigated")
    assert (await database.get_task(task.id)).status == "running"
    await database.close()

    restarted = Database(runtime_dir / "yuqing.db")
    await restarted.initialize()
    assert await restarted.mark_orphaned_tasks() == 1
    resumed = M0Orchestrator(
        restarted,
        EventBus(restarted),
        search=search,
        fetcher=AlwaysFailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agent=FixtureAgent(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
    )
    await resumed.resume_task(task.id)

    finished = await restarted.get_task(task.id)
    report = await restarted.get_report_for_task(task.id)
    events = await resumed.events.history(task.id)
    html = (runtime_dir / "reports" / f"{report['id']}.html").read_text(encoding="utf-8")
    assert finished.status == "done"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert "已证实" in html
    assert "原文抓取失败" in html
    assert 'href="#evidence-E001"' in html
    await restarted.close()


@pytest.mark.asyncio
async def test_agent_reflection_can_drive_a_second_inner_round(runtime_dir):
    database = Database(runtime_dir / "rounds.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="两轮事件", depth="quick"))
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://source.example/item",
                title="固定证据",
                snippet="用于验证两轮循环。",
                source_name="固定来源",
                provider="fixture",
            )
        ]
    )
    agent = TwoRoundAgent()
    orchestrator = M0Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=AlwaysFailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agent=agent,
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        max_inner_rounds=2,
    )

    await orchestrator.run_task(task.id, halt_after_checkpoint="investigated")

    claims = await database.list_claims(task.id)
    rounds = [
        event.data
        for event in await orchestrator.events.history(task.id)
        if event.event == "loop.round"
    ]
    assert [claim.round for claim in claims] == [1, 2]
    assert [item["decision"] for item in rounds] == ["continue", "stop"]
    assert search.calls == 2
    await database.close()


@pytest.mark.asyncio
async def test_resume_from_verified_checkpoint_only_builds_report(runtime_dir):
    database = Database(runtime_dir / "verified-resume.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="核验后续跑", depth="quick"))
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.xinhuanet.com/verified",
                title="已核验来源",
                snippet="核验后续跑已有公开证据。",
                source_name="新华社",
                provider="fixture",
            )
        ]
    )
    first = M0Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=AlwaysFailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agent=FixtureAgent(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
    )
    await first.run_task(task.id, halt_after_checkpoint="verified")
    await database.close()

    restarted = Database(runtime_dir / "verified-resume.db")
    await restarted.initialize()
    await restarted.mark_orphaned_tasks()
    resumed = M0Orchestrator(
        restarted,
        EventBus(restarted),
        search=search,
        fetcher=AlwaysFailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agent=FixtureAgent(),
        verifier=MustNotRunVerifier(),
        reports_dir=runtime_dir / "reports",
    )
    await resumed.resume_task(task.id)

    assert (await restarted.get_task(task.id)).status == "done"
    assert await restarted.get_report_for_task(task.id) is not None
    await restarted.close()
