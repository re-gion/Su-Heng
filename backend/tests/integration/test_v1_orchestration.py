from datetime import datetime

import pytest

from yuqing.agents.runtime import GeneratedClaim, InvestigationPlan, Reflection
from yuqing.core.events import EventBus
from yuqing.core.search.base import SearchResult
from yuqing.core.search.fixture import FixtureSearchProvider
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
    async def fetch(self, url):
        raise RuntimeError("不影响 snippet 核验")


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
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=search,
        fetcher=FailFetcher(),
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
