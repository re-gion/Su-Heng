import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from httpx import Request, Response
from openai import RateLimitError

from yuqing.agents.runtime import GeneratedClaim, InvestigationPlan, Reflection, SearchQuery
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchResult
from yuqing.core.llm.gateway import LLMBudgetExhausted
from yuqing.core.search.base import SearchResult
from yuqing.core.search.chain import SearchChain
from yuqing.core.search.fixture import FixtureSearchProvider
from yuqing.services.comment_plugin import CommentCandidateInput, CommentPluginService
from yuqing.services.forum import ForumBoard
from yuqing.services.historical_data import (
    DatasetAssetInput,
    HistoricalDataService,
    HistoricalEventInput,
)
from yuqing.services.moderation import ModeratorReview, ReviewDirective
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


@pytest.mark.asyncio
async def test_comment_review_reason_is_local_and_not_in_forum_payload(runtime_dir):
    from yuqing.storage.models import EvidenceCreate

    private_reason = "仅保存在本地、未经过展示审查的具体理由"

    class CommentAgent:
        async def analyze(self, event_query, rows, **kwargs):
            await kwargs["save_review_decision"](
                "fixture-review", {"accepted": False, "reason": private_reason}
            )
            return {
                "status": "failed",
                "items": [],
                "diagnostics": [{"category": "review_rejected", "message": "风险研判缺少样本支持"}],
                "coverage": {},
            }

    db = Database(runtime_dir / "comment-review-reasons.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(event_query="机构公开通报", investigation_scope="general")
        )
        await db.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.test/comments",
                title="评论样本",
                snippet="确认帖子的评论样本。",
                kind="social_comments",
                extra={"collection_id": "fixture-collection"},
            )
        )
        board = await ForumBoard.restore(db, task.id)

        async def post(board, message):
            return await board.post(message)

        runner = SimpleNamespace(
            database=db, events=EventBus(db), comment_agent=CommentAgent(), _post=post
        )
        await V1Orchestrator._run_comment_insight_impl(runner, task.id, task.event_query, board)
        local = await db.checkpoint(task.id, "comments:theme-review:fixture-review")
        assert local["reason"] == private_reason
        message = board.history()[-1]
        assert "风险研判缺少样本支持" in json.dumps(message.payload, ensure_ascii=False)
        assert private_reason not in json.dumps(message.payload, ensure_ascii=False)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_new_directive_reexamines_cached_material_once(runtime_dir):
    from yuqing.agents.openai_runtime import OpenAIInvestigationAgent
    from yuqing.services.forum import ForumMessageCreate

    class CachedAgent(OpenAIInvestigationAgent):
        drafts = 0

        async def plan(self, event_query):
            return InvestigationPlan(queries=["机构公开通报"])

        async def _summarize_uncached(self, event_query, evidence, *, claim_limit=8):
            self.drafts += 1
            return [
                GeneratedClaim(
                    text=f"机构通报公开事实第{self.drafts}项。", evidence_ids=[evidence[0].local_id]
                )
            ]

        async def reflect(self, event_query, claims):
            return Reflection(
                new_key_findings=[],
                remaining_gaps=[],
                next_queries=[],
                should_continue=False,
                reason="完成",
            )

    db = Database(runtime_dir / "directive-cache.db")
    await db.initialize()
    try:
        task = await db.create_task(TaskCreate(event_query="机构公开通报", source_languages=["zh"]))
        agent = CachedAgent(SimpleNamespace(), "system")
        runner = V1Orchestrator(
            db,
            EventBus(db),
            search=FixtureSearchProvider(
                [
                    SearchResult(
                        url="https://example.edu/notice",
                        title="机构公开通报",
                        snippet="机构公开通报及复核结果。",
                        provider="fixture",
                    )
                ]
            ),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": agent},
            moderator=ReleaseModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
            max_inner_rounds=1,
        )
        board = await ForumBoard.restore(db, task.id)

        async def run(round_number):
            await runner._run_agent(
                task_id=task.id,
                event_query=task.event_query,
                agent_name="fact_investigator",
                agent=agent,
                board=board,
                outer_round=round_number,
                top_k=2,
            )

        await run(1)
        await board.post(
            ForumMessageCreate(
                task_id=task.id,
                round=1,
                agent="moderator",
                type="directive",
                content="从已取得的通报补充复核结果。",
                payload={"agent": "fact_investigator"},
            )
        )
        await run(2)
        await run(2)
        assert agent.drafts == 2
        assert len(await db.list_claims(task.id)) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_agent_phase_budget_stop_keeps_material_without_failure_warning(runtime_dir):
    class BudgetLimitedAgent(NamedAgent):
        async def plan(self, event_query):
            raise LLMBudgetExhausted("LLM token budget exhausted")

    database = Database(runtime_dir / "agent-phase-budget.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="机构公开通报", depth="standard"))
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=FixtureSearchProvider([]),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": BudgetLimitedAgent("fact_investigator")},
            moderator=ReleaseModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
            max_outer_rounds=1,
        )
        await orchestrator.run_task(task.id)
        warnings = [
            e.data for e in await orchestrator.events.history(task.id) if e.event == "warning"
        ]
        reserved = next(e for e in warnings if e.get("code") == "AGENT_BUDGET_RESERVED")
        assert reserved["phase_token_limit"] == 840_000
        assert reserved["task_token_limit"] == 1_400_000
        assert "无法预留下一次" in reserved["message"]
        assert not any(e.get("code") == "AGENT_FAILED" for e in warnings)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_standard_reserves_llm_budget_for_verification_and_report(runtime_dir):
    usage = SimpleNamespace(tokens_used=0, calls=0, token_limit=None)
    observed = []

    class BudgetAgent(NamedAgent):
        async def plan(self, event_query):
            observed.append(("investigation", usage.token_limit))
            return await super().plan(event_query)

        async def reflect(self, event_query, claims):
            usage.tokens_used = 835_000
            return await super().reflect(event_query, claims)

    class HoldModerator:
        async def review(self, event_query, forum, evidence_count, claim_count):
            return ModeratorReview(release=False, reason="仍需核验")

    class BudgetVerifier:
        model_name = "fixture-verifier"

        async def verify(self, claim, evidence):
            observed.append(("verification", usage.token_limit))
            return VerificationRelation(
                relation="support", reason="材料支持", cited_sentence=evidence.snippet
            )

    database = Database(runtime_dir / "phase-budget.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(event_query="武汉大学图书馆事件", depth="standard")
        )
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=FixtureSearchProvider(
                [
                    SearchResult(
                        url="https://www.whu.edu.cn/info/1112/1234.htm",
                        title="武汉大学图书馆事件情况通报",
                        snippet="武汉大学发布图书馆事件情况通报。",
                        provider="fixture",
                        lang="zh",
                    )
                ]
            ),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": BudgetAgent("fact_investigator")},
            moderator=HoldModerator(),
            verifier=BudgetVerifier(),
            reports_dir=runtime_dir / "reports",
            usage=usage,
            max_outer_rounds=1,
            max_inner_rounds=1,
        )
        await orchestrator.run_task(task.id)
        assert ("investigation", 840_000) in observed
        assert ("verification", 1_260_000) in observed
        assert ("investigation", 1_260_000) in observed
        assert usage.token_limit == 1_400_000
        outcome = await database.checkpoint(task.id, "investigation:outcome")
        assert outcome["end_reason"] == "verification_reserve"
        reviews = [
            event.data
            for event in await orchestrator.events.history(task.id)
            if event.event == "host.review"
        ]
        assert any("预算已预留" in review["reason"] for review in reviews)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_standard_empty_investigation_recovers_twice_without_false_release(runtime_dir):
    database = Database(runtime_dir / "empty-recovery.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="机构公开通报", depth="standard"))
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=FixtureSearchProvider([]),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": FailingAgent("fact_investigator")},
            moderator=ReleaseModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
            max_outer_rounds=1,
            max_inner_rounds=1,
        )
        await orchestrator.run_task(task.id)
        recovery = await database.checkpoint(task.id, "report:quality_recovery")
        assert recovery["round"] == recovery["no_gain"] == 2
        assert recovery["end_reason"] == "no_progress"
        failed = await database.checkpoint(task.id, "report:recovery:fact_investigator:2")
        assert failed["status"] == "failed"
        assert failed["diagnostic"]["stage"] == "quality_recovery"
        statuses = [
            e.data["phase"]
            for e in await orchestrator.events.history(task.id)
            if e.event == "agent.status" and e.data["agent"] == "fact_investigator"
        ]
        assert statuses[-1] == "failed"
        report = json.loads((await database.get_report_for_task(task.id))["ir_json"])
        assert report["quality"]["release_label"] == "retrieval_diagnostic"
        assert (await database.get_task(task.id)).status == "done"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_quality_recovery_does_not_start_when_only_report_reserve_remains(runtime_dir):
    database = Database(runtime_dir / "recovery-budget.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="机构公开通报", depth="standard"))
        usage = SimpleNamespace(tokens_used=1_250_000, calls=1, token_limit=None)
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=FixtureSearchProvider([]),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": FailingAgent("fact_investigator")},
            moderator=ReleaseModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
            usage=usage,
            max_outer_rounds=1,
            max_inner_rounds=1,
        )
        await orchestrator.run_task(task.id)
        recovery = await database.checkpoint(task.id, "report:quality_recovery")
        assert recovery.get("round", 0) == 0
        assert recovery["end_reason"] == "budget_exhausted"
        assert await database.checkpoint(task.id, "report:recovery:fact_investigator:1") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stopped_task_does_not_restart_quality_recovery(runtime_dir):
    database = Database(runtime_dir / "stopped-recovery.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="机构公开通报", depth="standard"))
        await database.set_task_status(task.id, "stopping", "forum")
        agent = NamedAgent("fact_investigator")
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=FixtureSearchProvider([]),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": agent},
            moderator=ReleaseModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
        )
        await orchestrator.run_task(task.id)
        assert not agent.plan_inputs
        recovery = await database.checkpoint(task.id, "report:quality_recovery")
        assert recovery["end_reason"] == "user_stop"
        assert not recovery.get("round")
        report = json.loads((await database.get_report_for_task(task.id))["ir_json"])
        assert report["quality"]["investigation_outcome"]["end_reason"] == "user_stop"
    finally:
        await database.close()


def rate_limit_error():
    response = Response(
        429,
        request=Request("POST", "https://relay.example/v1/chat/completions"),
        json={"error": {"code": "rate_limit_exceeded", "message": "private key detail"}},
    )
    return RateLimitError("private key detail", response=response, body=response.json())


class RateLimitedPlanAgent(NamedAgent):
    async def plan(self, event_query):
        raise rate_limit_error()


class RateLimitedSummaryAgent(NamedAgent):
    async def summarize(self, event_query, evidence):
        raise rate_limit_error()


class RateLimitedReflectionAgent(NamedAgent):
    async def reflect(self, event_query, claims):
        raise rate_limit_error()


@pytest.mark.parametrize(
    ("agent_type", "code", "expected_claims", "phase"),
    [
        (RateLimitedPlanAgent, "AGENT_PLAN_RATE_LIMITED", 0, "blocked"),
        (RateLimitedSummaryAgent, "AGENT_SUMMARY_RATE_LIMITED", 0, "done"),
        (RateLimitedReflectionAgent, "AGENT_REFLECTION_RATE_LIMITED", 1, "done"),
    ],
)
@pytest.mark.asyncio
async def test_agent_429_preserves_prior_work_and_explains_stage(
    runtime_dir, agent_type, code, expected_claims, phase
):
    database = Database(runtime_dir / "reflection-429.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="公开通报事件", depth="quick"))
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=FixtureSearchProvider(
            [
                SearchResult(
                    url="https://www.gov.cn/notice",
                    title="公开通报事件调查结果",
                    snippet="主管部门公布公开通报事件的调查结果。",
                    provider="fixture",
                )
            ]
        ),
        fetcher=FailFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": agent_type("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        max_inner_rounds=1,
    )
    result = await orchestrator._run_agent(
        task_id=task.id,
        event_query=task.event_query,
        agent_name="fact_investigator",
        agent=orchestrator.agents["fact_investigator"],
        board=await ForumBoard.restore(database, task.id),
        outer_round=1,
        top_k=8,
    )

    events = await orchestrator.events.history(task.id)
    assert result["status"] == "partial"
    assert len(await database.list_claims(task.id)) == expected_claims
    assert any(
        event.event == "warning"
        and event.data.get("code") == code
        and event.data.get("rate_limit", {}).get("category") == "rate"
        and "private" not in event.data.get("message", "")
        for event in events
    )
    assert any(
        event.event == "agent.status" and event.data.get("phase") == phase for event in events
    )
    await database.close()


class ReleaseModerator:
    async def review(self, event_query, forum, evidence_count, claim_count):
        return ModeratorReview(release=True, reason="已有两个 Agent 完成，允许出报告")


@pytest.mark.parametrize(
    ("release", "allow_next_round", "expected_directives"),
    [(True, True, 0), (False, False, 0), (False, True, 1)],
)
@pytest.mark.asyncio
async def test_moderator_only_posts_directives_for_a_real_next_forum_round(
    runtime_dir, release, allow_next_round, expected_directives
):
    class DirectiveModerator:
        async def review(self, event_query, forum, evidence_count, claim_count):
            return ModeratorReview(
                release=release,
                reason="评审完成",
                directives=[ReviewDirective(agent="history_insight", instruction="补查独立案例")],
            )

    database = Database(runtime_dir / "moderator-directives.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="公开事件"))
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=FixtureSearchProvider([]),
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={},
            moderator=DirectiveModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
        )
        board = await ForumBoard.restore(database, task.id)
        review = await orchestrator._moderate(
            task.id, task.event_query, board, 2, allow_next_round=allow_next_round
        )
        messages = board.history(round_number=2)
        assert len(review.directives) == expected_directives
        assert [item.type for item in messages] == ["review"] + ["directive"] * expected_directives
        if expected_directives:
            assert messages[-1].payload == {"agent": "history_insight"}
            assert "补查独立案例" in board.digest_for("history_insight", 2)
            assert "补查独立案例" not in board.digest_for("fact_investigator", 2)
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ["fact_investigator", "history_insight"])
async def test_agent_searches_priority_window_then_explicit_outside_context(
    runtime_dir, agent_name
):
    class RecordingSearch:
        name = "recording"
        capabilities = {"freshness"}

        def __init__(self):
            self.calls = []

        async def search(self, params):
            self.calls.append((params.query, params.freshness))
            return []

    class PlannedAgent(NamedAgent):
        async def plan(self, event_query):
            return InvestigationPlan(
                queries=[
                    SearchQuery(query="事件窗口内通报", language="zh", scope="window"),
                    SearchQuery(query="event report", language="en", region="US"),
                    *(
                        [SearchQuery(query="事件较早起因", language="zh", scope="context")]
                        if agent_name == "fact_investigator"
                        else []
                    ),
                ]
            )

    database = Database(runtime_dir / "window-search.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(
                event_query="公开事件",
                time_range={"from": "2025-07-01", "to": "2025-07-31"},
            )
        )
        search = RecordingSearch()
        agent = PlannedAgent(agent_name)
        orchestrator = V1Orchestrator(
            database,
            EventBus(database),
            search=search,
            fetcher=FailFetcher(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={agent_name: agent},
            moderator=ReleaseModerator(),
            verifier=FixtureVerifier(),
            reports_dir=runtime_dir / "reports",
        )
        await orchestrator._run_agent(
            task_id=task.id,
            event_query=task.event_query,
            agent_name=agent_name,
            agent=agent,
            board=await ForumBoard.restore(database, task.id),
            outer_round=1,
            top_k=5,
        )
        assert search.calls == [
            ("事件窗口内通报", "2025-07-01..2025-07-31"),
            (
                "event report",
                "noLimit" if agent_name == "history_insight" else "2025-07-01..2025-07-31",
            ),
            *([("事件较早起因", "noLimit")] if agent_name == "fact_investigator" else []),
        ]
    finally:
        await database.close()


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


class StaticProvider:
    capabilities = {"freshness", "domain_filter", "publish_time"}

    def __init__(self, name: str, results: list[SearchResult]):
        self.name = name
        self.results = results
        self.calls = 0

    async def search(self, params):
        self.calls += 1
        return self.results


class DatedPageFetcher:
    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, url: str):
        self.calls.append(url)
        return FetchResult(
            url=url,
            html=('<meta property="article:published_time" content="2025-09-20T10:12:00+08:00">'),
            content_text=(
                "发布时间：2025年9月20日。武汉大学通报图书馆事件调查复核情况，"
                "学校组建专家组开展调查复核。"
            ),
            content_type="text/html",
        )


class MultiEvidenceAgent(NamedAgent):
    async def summarize(self, event_query, evidence):
        return [
            GeneratedClaim(
                text=f"{event_query}已有两个独立公开来源报道。",
                evidence_ids=[item.local_id for item in evidence],
            )
        ]


@pytest.mark.asyncio
async def test_selected_candidate_official_source_is_seeded_once_before_investigation(runtime_dir):
    database = Database(runtime_dir / "selected-source.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="武汉大学舆情",
            depth="standard",
            time_range={"from": "2023-01-01", "to": "2026-01-01"},
        )
    )
    query = "武汉大学通报图书馆事件调查复核情况"
    await database.set_resolved_event_query(task.id, query)
    await database.save_checkpoint(
        task.id,
        "topic:selected",
        {
            "phase": "outer",
            "next_outer_round": 1,
            "resolved_event_query": query,
            "selected_candidate": {
                "title": query,
                "summary": query + "，学校组建调查复核专家组。",
                "sources": [
                    {
                        "url": "https://www.whu.edu.cn/info/5231/258444.htm",
                        "title": "情况通报",
                        "source_name": "武汉大学",
                        "role": "party",
                        "provider": "qianfan",
                        "published_at": "2025-09-20T08:53:00",
                    },
                    {
                        "url": "https://xxgk.hubu.edu.cn/example",
                        "title": "转述武汉大学情况通报",
                        "source_name": "武汉大学",
                        "role": "party",
                        "provider": "qianfan",
                        "published_at": "2025-09-20T08:53:00",
                    },
                ],
            },
        },
    )
    fetcher = DatedPageFetcher()
    orchestrator = V1Orchestrator(
        database,
        EventBus(database),
        search=StaticProvider("fixture", []),
        fetcher=fetcher,
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
    )

    selected_task = await database.get_task(task.id)
    assert selected_task is not None
    assert orchestrator.evidence.classifier.institution_for("www.whu.edu.cn") == "武汉大学"
    assert orchestrator.evidence.classifier.institution_for("xxgk.hubu.edu.cn") is None
    await orchestrator._seed_selected_sources(selected_task)
    await orchestrator._seed_selected_sources(selected_task)

    evidence = await database.list_evidence(task.id)
    assert len(evidence) == 2
    by_url = {item.url: item for item in evidence}
    official = by_url["https://www.whu.edu.cn/info/5231/258444.htm"]
    other_school = by_url["https://xxgk.hubu.edu.cn/example"]
    assert official.source_role == "party"
    assert other_school.source_role == "unknown"
    assert all(item.fetch_status == "fetched" for item in evidence)
    assert all(item.extra["scope_status"] == "main" for item in evidence)
    assert fetcher.calls == list(by_url)
    await database.close()


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
    def __init__(self):
        self.refine_input = ""

    async def refine_query(self, event_query, context):
        self.refine_input = event_query
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
async def test_selected_event_falls_back_past_noise_and_clears_retrieval_gate(runtime_dir):
    database = Database(runtime_dir / "selected-event-retrieval.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="武汉大学舆情",
            depth="standard",
            time_range={"from": "2023-01-01", "to": "2026-01-01"},
        )
    )
    await database.set_resolved_event_query(task.id, "武汉大学通报图书馆事件调查复核情况")
    primary = StaticProvider(
        "langsearch",
        [
            SearchResult(
                url="https://xxgk.hubu.edu.cn/info/1234/5678.htm",
                title="新生复查期间有关举报、调查及处理结果（2023年）",
                snippet=("湖北大学新生复查正常完成。" + "普通正文。" * 80 + "相关链接：武汉大学。"),
                provider="langsearch",
            )
        ],
    )
    provider_text = "武汉大学通报图书馆事件调查复核情况。" * 40
    fallback = StaticProvider(
        "qianfan",
        [
            SearchResult(
                url="https://china.caixin.com/2025-09-20/example.html",
                title="武大通报图书馆事件调查复核情况",
                snippet="武汉大学通报图书馆事件调查复核情况",
                provider="qianfan",
                published_at=datetime(2025, 9, 20),
                content_text=provider_text,
                content_origin="provider_fulltext",
            ),
            SearchResult(
                url="https://www.news.cn/20250920/example.htm",
                title="武汉大学通报图书馆事件调查复核情况",
                snippet="武汉大学通报图书馆事件调查复核情况",
                provider="qianfan",
                published_at=datetime(2025, 9, 20),
                content_text=provider_text,
                content_origin="provider_fulltext",
            ),
        ],
    )
    fetcher = DatedPageFetcher()
    events = EventBus(database)
    orchestrator = V1Orchestrator(
        database,
        events,
        search=SearchChain([primary, fallback]),
        fetcher=fetcher,
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": MultiEvidenceAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await orchestrator.run_task(task.id)

    evidence = await database.list_evidence(task.id)
    claims = await database.list_claims(task.id)
    report = await database.get_report_for_task(task.id)
    search_events = [
        event for event in await events.history(task.id) if event.event == "search.result"
    ]
    assert primary.calls >= 1
    assert fallback.calls >= 1
    assert len(fetcher.calls) == 2
    assert {item.source_role for item in evidence} == {"independent"}
    assert {item.extra["scope_status"] for item in evidence} == {"main"}
    assert all(item.fetch_status == "fetched" for item in evidence)
    assert claims[0].badge == "verified"
    assert search_events[0].data["continued_from"] == "langsearch"
    assert search_events[0].data["degraded_from"] is None
    assert search_events[0].data["provider_diagnostics"][0]["status"] == (
        "relevance_filtered_empty"
    )
    first_attempt, recovery = search_events[0].data["provider_diagnostics"][:2]
    assert first_attempt["freshness"] == "2023-01-01..2026-01-01"
    assert recovery["provider"] == "langsearch"
    assert recovery["freshness"] == "noLimit"
    assert recovery["recovery_reason"] == "relax_preferred_date_window"
    # Counts belong to each attempt, rather than being copied from the provider total.
    assert first_attempt["rejected"] == {"subject_mismatch": 1}
    assert recovery["rejected"] == {"subject_mismatch": 1}
    report_ir = __import__("json").loads(report["ir_json"])
    assert report_ir["quality"]["release_label"] != "retrieval_diagnostic"
    await database.close()


@pytest.mark.asyncio
async def test_investigation_adds_exa_when_langsearch_results_share_one_publisher(runtime_dir):
    database = Database(runtime_dir / "search-source-coverage.db")
    await database.initialize()
    event_query = "武汉大学图书馆事件调查复核"
    task = await database.create_task(
        TaskCreate(
            event_query=event_query,
            depth="standard",
            source_languages=["zh"],
            time_range={"from": "2023-01-01", "to": "2026-01-01"},
        )
    )
    await database.set_resolved_event_query(task.id, event_query)
    langsearch = StaticProvider(
        "langsearch",
        [
            SearchResult(
                url=f"https://{host}/story-{index}",
                title=f"武汉大学图书馆事件调查复核进展 {index}",
                snippet="武汉大学图书馆事件调查复核进展。",
                provider="langsearch",
                lang="zh",
            )
            for index, host in enumerate(("www.news.cn", "www.xinhuanet.com"), start=1)
        ],
    )
    exa = StaticProvider(
        "exa",
        [
            SearchResult(
                url="https://www.people.com.cn/event-report",
                title="武汉大学图书馆事件调查复核进展",
                snippet="武汉大学图书馆事件调查复核进展。",
                provider="exa",
                lang="zh",
            )
        ],
    )
    qianfan = StaticProvider("qianfan", [])
    events = EventBus(database)
    orchestrator = V1Orchestrator(
        database,
        events,
        search=SearchChain([langsearch, exa, qianfan]),
        fetcher=DatedPageFetcher(),
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={"fact_investigator": MultiEvidenceAgent("fact_investigator")},
        moderator=ReleaseModerator(),
        verifier=FixtureVerifier(),
        reports_dir=runtime_dir / "reports",
        max_outer_rounds=1,
        max_inner_rounds=1,
    )

    await orchestrator.run_task(task.id)

    evidence = await database.list_evidence(task.id)
    search_events = [
        event for event in await events.history(task.id) if event.event == "search.result"
    ]
    assert {item.provider for item in evidence} == {"langsearch", "exa"}
    assert len(evidence) == 3
    assert (langsearch.calls, exa.calls, qianfan.calls) == (1, 1, 0)
    assert search_events[0].data["hits"] == 3
    assert search_events[0].data["raw_hits"] == 3
    assert search_events[0].data["provider"] == "exa"
    assert [item["status"] for item in search_events[0].data["provider_diagnostics"]] == [
        "insufficient_coverage",
        "success",
    ]
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
    report_done = [event.data for event in events if event.event == "report.done"]
    assert any(item.get("partial") and item.get("comment_pending") for item in report_done)
    assert report_done[-1].get("partial") is not True
    # The core report must be published before optional comment privacy review
    # starts; otherwise the two branches can compete for the same LLM budget.
    partial_seq = next(
        event.seq
        for event in events
        if event.event == "report.done" and event.data.get("comment_pending")
    )
    comment_phase_seq = next(
        event.seq
        for event in events
        if event.event == "task.status" and event.data.get("phase") == "comment_analysis"
    )
    assert partial_seq < comment_phase_seq
    # Resuming saved comments must not attribute their analysis to the forum.
    assert "comment_analysis" in phases
    assert "forum" not in phases[phases.index("comment_selection") + 1 :]
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
    await database.set_resolved_event_query(task.id, "武汉大学图书馆事件及校方回应")
    task = await database.get_task(task.id)
    assert task is not None
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

    discovery = await orchestrator._prepare_comment_candidates(
        task, task.resolved_event_query or ""
    )

    candidates = await plugin.list_candidates(task.id)
    assert discovery["candidate_count"] == 1
    assert evaluator.refine_input == "武汉大学图书馆事件及校方回应"
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
        "第 3 轮新增事实。",
    ]
    # Two investigation rounds remain intact; the missing report basis gets one
    # separately checkpointed recovery, never an unbounded inner loop.
    assert decisions == ["continue", "stop", "stop"]
    assert await database.checkpoint(task.id, "report:recovery:fact_investigator")
    assert search.calls == 3
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
    await first.events.emit(
        task.id,
        "warning",
        {
            "code": "AGENT_FAILED",
            "agent": "media_propagation",
            "message": "媒体 Agent 的反思请求受限，第二轮分析不完整。",
        },
    )
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
    report = await restarted.get_report_for_task(task.id)
    assert report is not None
    report_ir = json.loads(report["ir_json"])
    limitations = next(
        block["items"] for block in report_ir["blocks"] if block["type"] == "limitations"
    )
    assert any("第二轮分析不完整" in item["text"] for item in limitations)
    await restarted.close()
