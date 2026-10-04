from types import SimpleNamespace

import pytest

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchResult
from yuqing.core.search.base import SearchResult
from yuqing.core.search.chain import SearchChain
from yuqing.services.forum import ForumBoard
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.investigation_scope import ReportReleaseAssessment
from yuqing.services.v1_orchestrator import V1Orchestrator
from yuqing.storage.db import Database
from yuqing.storage.models import EvidenceCreate, TaskCreate
from yuqing.storage.snapshots import SnapshotStore

NOTICE_BODY = "情况通报\n甲乙大学公布复核程序与处理结果。" + "\n".join(
    f"第{i}项复核事项：专家查阅对应材料、访谈相关人员并记录程序依据；"
    f"公开说明第{i}项事项已经完成的工作、决定形成日期、负责人职责与仍需补充的说明。"
    for i in range(1, 13)
)


class SourceSearch:
    name = "fixture"
    capabilities = {"freshness", "publish_time"}

    def __init__(self):
        self.calls = []

    async def search(self, params):
        self.calls.append(params)
        return [
            SearchResult(
                url="https://school.example.edu/notice/42",
                title="情况通报-甲乙大学",
                snippet="甲乙大学官方网站发布情况通报。",
                source_name="甲乙大学官网",
                provider=self.name,
            )
        ]


class SourceFetcher:
    def __init__(self):
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        return FetchResult(
            url=url,
            html='<meta property="article:published_time" content="2025-09-20">',
            content_text=NOTICE_BODY,
            content_type="text/html",
        )


async def setup_recovery(runtime_dir):
    db = Database(runtime_dir / "source-recovery.db")
    await db.initialize()
    task = await db.create_task(
        TaskCreate(
            event_query="甲乙大学图书馆事件",
            depth="standard",
            source_languages=["zh"],
            time_range={"from": "2025-01-01", "to": "2025-12-31"},
        )
    )
    await db.set_task_status(task.id, "running", "reporting")
    await db.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://media.example/news/42",
            title="甲乙大学通报图书馆事件调查复核情况",
            snippet="甲乙大学图书馆事件调查复核情况通报。",
            source_name="媒体",
            source_role="independent",
            fetch_status="fetched",
            published_at="2025-09-20",
            fetched_at="2025-09-20",
            content_text=NOTICE_BODY,
            snapshot_path="fixture.html",
            content_sha256="fixture",
            extra={"scope_status": "main", "page_source_credits": ["来源：甲乙大学官网"]},
        )
    )
    search, fetcher = SourceSearch(), SourceFetcher()
    runner = V1Orchestrator(
        db,
        EventBus(db),
        search=SearchChain([search]),
        fetcher=fetcher,
        snapshots=SnapshotStore(runtime_dir / "snapshots"),
        agents={},
        moderator=SimpleNamespace(),
        verifier=SimpleNamespace(),
        reports_dir=runtime_dir / "reports",
    )
    return db, task, runner, search, fetcher


@pytest.mark.asyncio
async def test_recovery_fetches_credited_generic_notice_before_claim_generation(runtime_dir):
    db, task, runner, search, fetcher = await setup_recovery(runtime_dir)
    try:
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=1,
            missing=["media_propagation"],
        )
        evidence = await db.list_evidence(task.id)
        assert fetcher.calls == ["https://school.example.edu/notice/42"]
        assert len(search.calls) == 1
        assert "甲乙大学" in search.calls[0].query
        assert "2025-09-20" in search.calls[0].query
        original = next(e for e in evidence if "school.example.edu" in e.url)
        assert original.fetch_status == "fetched"
        assert original.extra["scope_status"] == "main"
        distractors = [
            original.model_copy(
                update={"local_id": f"D{i:03}", "publisher_entity": "甲乙大学官网", "extra": {}}
            )
            for i in range(5)
        ]
        bounded_context = FullReportBuilder._relation_source_context(
            [evidence[0], *distractors, original], {"E001": {"publisher": "媒体"}}
        )
        assert any(s["evidence_ref"] == original.local_id for s in bounded_context)
        context = FullReportBuilder._relation_source_context(evidence, {})

        class Gateway:
            async def complete_json(self, role, system, prompt, **kwargs):
                if role == "verifier":
                    return {"accepted": True}
                return {
                    "edges": [
                        {
                            "from_evidence_id": original.local_id,
                            "to_evidence_id": "E001",
                            "support_evidence_id": "E001",
                            "relation": "repost",
                            "quote": "来源：甲乙大学官网",
                        }
                    ]
                }

        relations = await OpenAIReportAgent(Gateway(), "report").recover_relations(context)
        release = ReportReleaseAssessment.evaluate(
            concrete_event=True,
            main_evidence=len(evidence),
            verifiable_key_claims=1,
            event_timeline_nodes=2,
            publication_nodes=2,
            propagation_edges=len(relations),
            summary_has_what=True,
            summary_has_why=True,
            summary_has_action=True,
            evidence_bound_recommendations=1,
        )
        assert release.label == "full_report"
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=2,
            missing=["media_propagation"],
        )
        assert len(search.calls) == len(fetcher.calls) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_investigation_reserves_tools_without_increasing_total(runtime_dir):
    db, task, runner, _, _ = await setup_recovery(runtime_dir)
    try:
        budget = runner.budget_for(task.depth)
        runner._search_calls = budget.search_calls - 6
        runner._fetch_calls = budget.fetch_calls - 6
        assert not await runner._reserve_tool("search", investigation=True)
        assert not await runner._reserve_tool("fetch", investigation=True)
        assert "剩余额度留给核心发布缺口补查" in runner._tool_budget_message(
            "search", investigation=True
        )
        assert await runner._reserve_tool("search")
        assert await runner._reserve_tool("fetch")
        runner._search_calls = budget.search_calls
        runner._fetch_calls = budget.fetch_calls
        assert not await runner._reserve_tool("search")
        assert not await runner._reserve_tool("fetch")
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["different_body", "different_date", "undated", "foreign"])
async def test_source_identity_cannot_bypass_body_date_or_region(runtime_dir, fault):
    db, task, runner, search, fetcher = await setup_recovery(runtime_dir)
    try:
        original_fetch = fetcher.fetch

        async def fetch(url):
            result = await original_fetch(url)
            if fault == "different_body":
                result = result.model_copy(
                    update={"content_text": "甲乙大学关于新生收费的情况通报。" * 50}
                )
            elif fault == "different_date":
                result = result.model_copy(
                    update={"html": '<meta property="article:published_time" content="2025-09-21">'}
                )
            elif fault == "undated":
                result = result.model_copy(update={"html": "<article>情况通报</article>"})
            return result

        fetcher.fetch = fetch
        if fault == "foreign":
            await db.execute_write("UPDATE task SET source_scope='domestic' WHERE id=?", (task.id,))
            original_search = search.search

            async def search_foreign(params):
                return [
                    r.model_copy(update={"url": "https://www.bbc.com/notices/42"})
                    for r in await original_search(params)
                ]

            search.search = search_foreign
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=1,
            missing=["media_propagation"],
        )
        originals = [e for e in await db.list_evidence(task.id) if e.local_id != "E001"]
        assert originals
        assert all(not (e.extra or {}).get("main_eligible") for e in originals)
        assert all("publication_identity" not in (e.extra or {}) for e in originals)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reviewer_rejection_still_prevents_complete_report(runtime_dir):
    db, task, runner, _, _ = await setup_recovery(runtime_dir)
    try:
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=1,
            missing=["media_propagation"],
        )
        evidence = await db.list_evidence(task.id)
        original = next(e for e in evidence if e.local_id != "E001")

        class RejectingGateway:
            async def complete_json(self, role, system, prompt, **kwargs):
                if role == "verifier":
                    return {"accepted": False}
                return {
                    "edges": [
                        {
                            "from_evidence_id": original.local_id,
                            "to_evidence_id": "E001",
                            "support_evidence_id": "E001",
                            "relation": "repost",
                            "quote": "来源：甲乙大学官网",
                        }
                    ]
                }

        context = FullReportBuilder._relation_source_context(evidence, {})
        relations = await OpenAIReportAgent(RejectingGateway(), "report").recover_relations(context)
        release = ReportReleaseAssessment.evaluate(
            concrete_event=True,
            main_evidence=2,
            verifiable_key_claims=1,
            event_timeline_nodes=2,
            publication_nodes=2,
            propagation_edges=len(relations),
            summary_has_what=True,
            summary_has_why=True,
            summary_has_action=True,
            evidence_bound_recommendations=1,
        )
        assert release.label == "evidence_brief"
        assert release.missing == ("media_propagation",)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stop_after_source_search_prevents_fetch(runtime_dir):
    db, task, runner, search, fetcher = await setup_recovery(runtime_dir)
    try:
        original_search = search.search

        async def stop_on_search(params):
            results = await original_search(params)
            await db.set_task_status(task.id, "stopping", "reporting")
            return results

        search.search = stop_on_search
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=1,
            missing=["media_propagation"],
        )
        assert not fetcher.calls
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_search_failure_preserves_sources_and_warning_on_resume(runtime_dir):
    db, task, runner, search, fetcher = await setup_recovery(runtime_dir)
    try:

        async def fail(params):
            raise RuntimeError("search unavailable")

        search.search = fail
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=1,
            missing=["media_propagation"],
        )
        assert len(await db.list_evidence(task.id)) == 1
        assert not fetcher.calls
        assert runner._search_calls == 2
        assert any(
            e.data.get("code") == "PUBLICATION_SOURCE_RECOVERY_FAILED"
            for e in await runner.events.history(task.id)
        )
        assert any("原始发布补查失败" in message for message in runner._limitations)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_source_lookup_skips_media_mirrors_and_falls_through(runtime_dir):
    db, task, runner, official, fetcher = await setup_recovery(runtime_dir)
    try:

        class Mirror:
            name = "mirror"
            capabilities = set()

            async def search(self, params):
                return [
                    SearchResult(
                        url="https://another-media.example/news/42",
                        title="甲乙大学通报图书馆事件调查复核情况",
                        source_name="另一家媒体",
                        snippet=NOTICE_BODY,
                        provider=self.name,
                    )
                ]

        runner.search = SearchChain([Mirror(), official])
        await runner._recover_report_gaps(
            task.id,
            task.event_query,
            await ForumBoard.restore(db, task.id),
            recovery_round=1,
            missing=["media_propagation"],
        )
        assert fetcher.calls == ["https://school.example.edu/notice/42"]
        assert runner._search_calls == 2
        assert len(await db.list_evidence(task.id)) == 2
    finally:
        await db.close()
