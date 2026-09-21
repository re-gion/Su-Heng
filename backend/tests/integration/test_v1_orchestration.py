from datetime import datetime
from types import SimpleNamespace

import pytest

from yuqing.agents.runtime import GeneratedClaim, InvestigationPlan, Reflection
from yuqing.core.events import EventBus
from yuqing.core.search.base import SearchResult
from yuqing.core.search.fixture import FixtureSearchProvider
from yuqing.services.comment_plugin import CommentCandidateInput, CommentPluginService
from yuqing.services.historical_data import (
    DatasetAssetInput,
    HistoricalDataService,
    HistoricalEventInput,
)
from yuqing.services.moderation import ModeratorReview
from yuqing.services.v1_orchestrator import V1Orchestrator
from yuqing.services.verifier import VerificationRelation
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate
from yuqing.storage.snapshots import SnapshotStore


class NamedAgent:
    def __init__(self, name: str):
        self.name = name
        self.plan_inputs = []

    async def plan(self, event_query):
        self.plan_inputs.append(event_query)
        return InvestigationPlan(queries=[f"{event_query} {self.name}"])

    async def summarize(self, event_query, evidence):
        return [
            GeneratedClaim(
                text=f"{self.name} 找到一条可核验公开材料。",
                evidence_ids=[evidence[0].local_id],
            )
        ]

    async def reflect(self, event_query, claims):
        return Reflection(
            new_key_findings=[self.name],
            remaining_gaps=[],
            next_queries=[],
            should_continue=False,
            reason="本轮已完成",
        )


class FailingAgent(NamedAgent):
    async def plan(self, event_query):
        raise RuntimeError("fixture provider 持续失败")


class ReleaseModerator:
    async def review(self, event_query, forum, evidence_count, claim_count):
        return ModeratorReview(release=True, reason="已有两个 Agent 完成，允许出报告")


class FixtureVerifier:
    model_name = "fixture-verifier"

    async def verify(self, claim, evidence):
        return VerificationRelation(
            relation="support",
            reason="材料支持",
            cited_sentence=evidence.snippet or "材料支持",
        )


class FailFetcher:
    def __init__(self):
        self.calls = 0

    async def fetch(self, url):
        self.calls += 1
        raise RuntimeError("不影响 snippet 核验")


class MultiEvidenceAgent(NamedAgent):
    async def summarize(self, event_query, evidence):
        return [
            GeneratedClaim(
                text=f"{event_query}已有两个独立公开来源报道。",
                evidence_ids=[item.local_id for item in evidence],
            )
        ]


class TwoRoundAgent(MultiEvidenceAgent):
    def __init__(self):
        super().__init__("fact_investigator")
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


class MustNotRunVerifier:
    model_name = "must-not-run"

    async def verify(self, claim, evidence):
        raise AssertionError("verified 检查点后不应重复核验")


class MustNotRunAgent(NamedAgent):
    async def plan(self, event_query):
        raise AssertionError("investigated 检查点后不应重复调查")


class PreciseCandidateEvaluator:
    async def refine_query(self, event_query, context):
        return "武汉大学图书馆事件"

    async def evaluate(self, event_query, candidates):
        assert event_query == "武汉大学图书馆事件"
        return {
            item.url: {"relevance": 0.9, "controversy": 0.4, "information_gain": 0.8}
            for item in candidates
        }


class PublicCandidateFixture:
    def __init__(self):
        self.query = ""
        self.platforms: list[str] = []

    async def discover(self, query, platforms, *, limit_per_platform=3):
        self.query = query
        self.platforms = platforms
        return [
            CommentCandidateInput(
                url="https://www.bilibili.com/video/BV1xx411c7mD",
                title="武汉大学图书馆事件梳理",
                snippet="公开讨论与事件时间线",
            )
        ]


@pytest.mark.asyncio
async def test_topic_discovery_persists_utility_usage_and_config_snapshot(runtime_dir):
    database = Database(runtime_dir / "topic-usage.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="武汉大学舆情",
            request_kind="topic_discovery",
            source_scope="domestic",
            source_languages=["zh"],
        )
    )
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.whu.edu.cn/example",
                title="武汉大学发布图书馆事件情况说明",
                snippet="武汉大学发布情况说明。",
                provider="fixture",
                lang="zh",
            )
        ]
    )
    usage = SimpleNamespace(tokens_used=37, calls=2, token_limit=0)
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        usage=usage,
        models_used={"utility": "fixture|utility"},
    )

    await orchestrator.run_task(task.id)

    updated = await database.get_task(task.id)
    events = await orchestrator.events.history(task.id)
    assert updated is not None
    assert updated.status == "paused"
    assert updated.phase == "topic_selection"
    assert updated.tokens_used == 37
    assert "fixture|utility" in (updated.config_snapshot or "")
    budget = next(event for event in events if event.event == "budget.update")
    assert budget.data["calls"] == 2
    await database.close()


@pytest.mark.asyncio
async def test_three_agents_isolate_failure_and_still_publish_full_report(runtime_dir):
    database = Database(runtime_dir / "v1.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="三 Agent 测试", depth="standard"))
    history_data = HistoricalDataService(database)
    await history_data.import_events(
        DatasetAssetInput(
            slug="orchestration-fixture",
            name="编排本地历史库",
            source_url="https://data.example.com/orchestration",
            license_label="fixture-only",
            upstream_rights_note="测试数据",
            redistribution="restricted",
        ),
        [
            HistoricalEventInput(
                event_name="三 Agent 测试",
                summary="当前覆盖事件。",
                nature="系统测试",
                source_url="https://history.example.com/anchor",
                keywords=["系统测试", "协作"],
            ),
            HistoricalEventInput(
                event_name="历史协作测试",
                summary="历史上进行过协作测试。",
                outcome="测试完成",
                nature="系统测试",
                source_url="https://history.example.com/similar",
                keywords=["系统测试", "协作"],
            ),
        ],
    )
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.gov.cn/notice",
                title="公开通报",
                snippet="该事件已有公开通报。",
                source_name="主管部门",
                provider="fixture",
                published_at=datetime(2026, 8, 12),
            )
        ]
    )
    history_agent = NamedAgent("history_insight")
    fetcher = FailFetcher()
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=fetcher,
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={
            "fact_investigator": NamedAgent("fact_investigator"),
            "media_propagation": FailingAgent("media_propagation"),
            "history_insight": history_agent,
        },
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        historical_data=history_data,
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await orchestrator.run_task(task.id)

    events = await orchestrator.events.history(task.id)
    report_row = await database.get_report_for_task(task.id)
    report = await database.get_report_for_task(task.id)
    assert (await database.get_task(task.id)).status == "done"
    assert {claim.agent for claim in await database.list_claims(task.id)} == {
        "fact_investigator",
        "history_insight",
    }
    assert any(event.event == "host.review" for event in events)
    assert sum(event.event == "budget.update" for event in events) >= 2
    assert any(
        event.event == "warning" and event.data.get("agent") == "media_propagation"
        for event in events
    )
    assert "本地历史库命中" in history_agent.plan_inputs[0]
    assert report_row is not None and report is not None
    assert len(__import__("json").loads(report["ir_json"])["blocks"]) >= 10
    assert fetcher.calls == 1
    await database.close()


@pytest.mark.asyncio
async def test_quick_depth_runs_only_fact_investigator(runtime_dir):
    database = Database(runtime_dir / "quick.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="快速调查", depth="quick"))
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.gov.cn/quick",
                title="快速公开材料",
                snippet="快速调查取得一条公开材料。",
                source_name="主管部门",
                provider="fixture",
            )
        ]
    )
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={
            "fact_investigator": NamedAgent("fact_investigator"),
            "media_propagation": NamedAgent("media_propagation"),
            "history_insight": NamedAgent("history_insight"),
        },
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await orchestrator.run_task(task.id)

    assert {claim.agent for claim in await database.list_claims(task.id)} == {"fact_investigator"}
    skipped = [
        event.data.get("agent")
        for event in await orchestrator.events.history(task.id)
        if event.event == "agent.status" and event.data.get("phase") == "skipped"
    ]
    assert skipped == ["media_propagation", "history_insight"]
    await database.close()


@pytest.mark.asyncio
async def test_v1_restart_after_investigation_resumes_and_publishes_report(runtime_dir):
    database = Database(runtime_dir / "restart.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="重启续跑事件",
            depth="quick",
            source_languages=["zh"],
            comment_mode="smart",
            comment_urls=["https://www.bilibili.com/video/BV1xx411c7mD"],
        )
    )
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.xinhuanet.com/restart",
                title="媒体甲报道",
                snippet="媒体甲确认重启续跑事件已有公开报道。",
                source_name="媒体甲",
                provider="fixture",
                published_at=datetime(2026, 8, 12),
            ),
            SearchResult(
                url="https://www.people.com.cn/restart",
                title="媒体乙报道",
                snippet="媒体乙确认重启续跑事件已有公开报道。",
                source_name="媒体乙",
                provider="fixture",
                published_at=datetime(2026, 8, 12),
            ),
        ]
    )
    comment_plugin = CommentPluginService(database, runtime_dir / "plugin", enabled=True)
    interrupted = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": MultiEvidenceAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        comment_plugin=comment_plugin,
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await interrupted.run_task(task.id)
    assert (await database.get_task(task.id)).status == "paused"
    assert (await database.latest_checkpoint(task.id))["phase"] == "comment_selection"
    await database.close()

    restarted = Database(runtime_dir / "restart.db")
    await restarted.initialize()
    resumed_plugin = CommentPluginService(restarted, runtime_dir / "plugin", enabled=True)
    assert await resumed_plugin.claim_selection(task.id, [], next_phase="comment_analysis")
    await restarted.save_checkpoint(
        task.id,
        "comments:ready",
        {"phase": "comments_ready", "completed": 0, "failed": 0},
    )
    resumed = V1Orchestrator(
        restarted,
        EventBus(restarted),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": MustNotRunAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        comment_plugin=resumed_plugin,
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await resumed.resume_task(task.id)

    report = await restarted.get_report_for_task(task.id)
    events = await resumed.events.history(task.id)
    html = (runtime_dir / "reports" / f"{report['id']}.html").read_text(encoding="utf-8")
    assert (await restarted.get_task(task.id)).status == "done"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert "已证实" in html
    assert "原文抓取失败" in html
    assert 'href="#evidence-E001"' in html
    phases = [event.data.get("phase") for event in events if event.event == "task.status"]
    assert "verifying" in phases
    assert "reporting" in phases
    await restarted.close()


@pytest.mark.asyncio
async def test_smart_comment_discovery_uses_refined_query_and_public_platform_search(
    runtime_dir,
):
    database = Database(runtime_dir / "candidate-discovery.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="武汉大学舆情", depth="standard", comment_mode="smart")
    )
    discoverer = PublicCandidateFixture()
    evaluator = PreciseCandidateEvaluator()
    plugin = CommentPluginService(
        database,
        runtime_dir / "plugin",
        enabled=True,
        discoverer=discoverer,
        evaluator=evaluator,
    )
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=FixtureSearchProvider(
            [
                SearchResult(
                    url="https://en.wikipedia.org/wiki/Wuhan_University",
                    title="无关站外结果",
                    snippet="不应进入评论候选",
                    provider="fixture",
                )
            ]
        ),
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": NamedAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        comment_plugin=plugin,
        comment_evaluator=evaluator,
    )

    discovery = await orchestrator._prepare_comment_candidates(task, task.event_query)

    candidates = await plugin.list_candidates(task.id)
    assert discovery["candidate_count"] == 1
    assert discoverer.query == "武汉大学图书馆事件"
    assert "bilibili" in discoverer.platforms
    assert [(item.platform, item.title) for item in candidates] == [
        ("bilibili", "武汉大学图书馆事件梳理")
    ]
    status = [
        event
        for event in await orchestrator.events.history(task.id)
        if event.event == "agent.status"
    ][-1]
    assert status.data["query"] == "武汉大学图书馆事件"
    assert status.data["platforms"] == ["bilibili"]
    await database.close()


@pytest.mark.asyncio
async def test_smart_comment_mode_with_no_candidates_pauses_for_manual_input(runtime_dir):
    database = Database(runtime_dir / "empty-candidates.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="没有平台帖的事件", depth="quick", comment_mode="smart")
    )
    search = FixtureSearchProvider(
        [
            SearchResult(
                url="https://www.gov.cn/notice",
                title="权威公开材料",
                snippet="该事件有公开材料，但不是社交平台帖子。",
                provider="fixture",
            )
        ]
    )
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": NamedAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        comment_plugin=CommentPluginService(database, runtime_dir / "plugin", enabled=True),
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await orchestrator.run_task(task.id)

    saved = await database.get_task(task.id)
    events = await orchestrator.events.history(task.id)
    assert saved is not None and saved.status == "paused"
    assert saved.phase == "comment_selection"
    assert any(
        event.event == "warning" and event.data.get("code") == "COMMENT_CANDIDATES_EMPTY"
        for event in events
    )
    assert any(
        event.event == "task.status" and event.data.get("phase") == "comment_selection"
        for event in events
    )
    await database.close()


@pytest.mark.asyncio
async def test_v1_agent_reflection_can_drive_a_second_inner_round(runtime_dir):
    database = Database(runtime_dir / "rounds.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="两轮事件", depth="quick", source_languages=["zh"])
    )
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
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": agent},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        max_outer_rounds=1,
        max_inner_rounds=2,
    )

    await orchestrator.run_task(task.id)

    decisions = [
        event.data["decision"]
        for event in await orchestrator.events.history(task.id)
        if event.event == "loop.round" and event.data.get("scope") == "inner"
    ]
    assert [claim.text for claim in await database.list_claims(task.id)] == [
        "第 1 轮新增事实。",
        "第 2 轮新增事实。",
    ]
    assert decisions == ["continue", "stop"]
    assert search.calls == 2
    await database.close()


@pytest.mark.asyncio
async def test_v1_resume_from_verified_checkpoint_only_builds_report(runtime_dir):
    database = Database(runtime_dir / "verified-resume.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="核验后续跑", depth="quick", source_languages=["zh"])
    )
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
    blocked_reports = runtime_dir / "blocked-reports"
    blocked_reports.write_text("阻止首次报告写入", encoding="utf-8")
    first = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": MultiEvidenceAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=blocked_reports,
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    with pytest.raises(FileExistsError):
        await first.run_task(task.id)
    assert (await database.latest_checkpoint(task.id))["phase"] == "verified"
    await database.close()

    restarted = Database(runtime_dir / "verified-resume.db")
    await restarted.initialize()
    assert await restarted.mark_orphaned_tasks() == 1
    resumed = V1Orchestrator(
        restarted,
        EventBus(restarted),
        search=search,
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": MustNotRunAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=MustNotRunVerifier(),
        reports_dir=runtime_dir / "reports",
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await resumed.resume_task(task.id)

    assert (await restarted.get_task(task.id)).status == "done"
    assert await restarted.get_report_for_task(task.id) is not None
    await restarted.close()
