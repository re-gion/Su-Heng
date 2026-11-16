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


@pytest.mark.parametrize("prefix", ["2023年", "2023年6月17日"])
def test_publication_network_uses_same_date_free_subject_identity_as_investigation(prefix):
    sources = {
        ref: SimpleNamespace(
            title="甲乙大学食品安全事件通报",
            content_text="甲乙大学公布调查结果。",
            published_at="2023-06-17",
            snippet="",
        )
        for ref in ("E001", "E002")
    }
    claims = [
        SimpleNamespace(
            agent="media_propagation",
            local_id=f"C{i:03d}",
            text="媒体发布调查结果",
            analysis_data={
                "publication_node": {
                    "evidence_id": ref,
                    "publisher": f"媒体{i}",
                    "node_type": "repost",
                },
                "propagation_edges": [
                    {"from_evidence_id": "E001", "to_evidence_id": "E002", "relation": "repost"}
                ],
            },
        )
        for i, ref in enumerate(sources, 1)
    ]
    network, nodes, edges = FullReportBuilder._publication_network(
        claims,
        sources,
        set(sources),
        {"C001": {"E001"}, "C002": {"E002"}},
        {"items": []},
        f"{prefix}甲乙大学食堂异物事件",
    )
    assert nodes == 2 and edges == 1
    assert {n["evidence_id"] for n in network["nodes"]} == set(sources)


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


@pytest.mark.asyncio
async def test_source_lookup_uses_article_title_and_ignores_platform_credit(runtime_dir):
    db, task, runner, search, _ = await setup_recovery(runtime_dir)
    try:
        title = "甲乙大学图书馆事件复核通报"
        await db.execute_write(
            "UPDATE evidence SET title=?,content_text=?,extra=? WHERE task_id=? AND local_id='E001'",
            (
                title + "-报道平台",
                "澎湃Logo\n首页 热点 登录\n" + NOTICE_BODY,
                '{"scope_status":"main","page_source_credits":'
                '["来源：澎湃新闻·澎湃号·媒体","来源/甲乙大学官网"]}',
                task.id,
            ),
        )
        await runner._recover_publication_sources(task, await db.list_evidence(task.id))
        assert len(search.calls) == 1
        query = search.calls[0].query
        assert task.event_query in query
        assert "情况通报" in query
        assert "甲乙大学" in query
        assert "澎湃Logo" not in query and "澎湃号" not in query
        assert "报道平台" not in query
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_official_notice_lookup_uses_its_heading_with_event_and_checks_dated_copy(
    runtime_dir,
):
    db, task, runner, search, fetcher = await setup_recovery(runtime_dir)
    try:
        for i, title in enumerate(("【栏目】复核结果全文", "刚刚，学校回应了", "该校发布最新消息")):
            await db.add_evidence(
                EvidenceCreate(
                    task_id=task.id,
                    url=f"https://mirror{i}.example/news/42",
                    title=title,
                    source_name=f"媒体{i}",
                    fetch_status="fetched",
                    published_at="2025-09-20",
                    fetched_at="2025-09-20",
                    content_text=f"媒体{i}报道\n" + NOTICE_BODY,
                    snapshot_path=f"mirror{i}.html",
                    content_sha256=f"mirror{i}",
                    extra={"scope_status": "main", "page_source_credits": ["来源/甲乙大学官网"]},
                )
            )
        original_search = search.search

        async def search_by_heading(params):
            if "情况通报" not in params.query or '"' in params.query:
                search.calls.append(params)
                return []
            return await original_search(params)

        search.search = search_by_heading
        await runner._recover_publication_sources(task, await db.list_evidence(task.id))

        assert len(search.calls) == 1
        assert task.event_query in search.calls[0].query
        assert fetcher.calls == ["https://school.example.edu/notice/42"]
        original = next(e for e in await db.list_evidence(task.id) if e.url in fetcher.calls)
        assert original.extra["scope_status"] == "main"
        assert original.extra["publication_identity"]["credited_evidence_id"] == "E001"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_recovery_skips_cross_language_current_event_and_undated_lead(runtime_dir):
    db, task, runner, _, _ = await setup_recovery(runtime_dir)
    try:
        refs = []
        for i, (title, provenance) in enumerate(
            (
                ("University reopens the same library case", "page_visible"),
                ("Review of a different university case", "search_provider"),
                ("Court publishes an earlier student disciplinary ruling", "page_visible"),
            )
        ):
            record = await db.add_evidence(
                EvidenceCreate(
                    task_id=task.id,
                    url=f"https://history{i}.example/ruling",
                    title=title,
                    lang="en",
                    fetch_status="fetched",
                    source_tier=2,
                    published_at="2025-03-01",
                    fetched_at="2025-09-20",
                    content_text=title,
                    snapshot_path=f"history{i}.html",
                    content_sha256=f"history{i}",
                    extra={"scope_status": "history", "date_provenance": provenance},
                )
            )
            refs.append(record.local_id)

        async def subject_alias(topic, source):
            assert topic == task.event_query
            return "University" if "same library case" in source.title else None

        async def fetched(record, **_kwargs):
            return record

        gathered = []

        class History:
            async def summarize(self, _query, sources):
                gathered.extend(s.local_id for s in sources)
                return []

        runner.search_planning.subject_alias = subject_alias
        runner._fetch_once = fetched
        runner.agents["history_insight"] = History()
        await runner._repair_history_comparison(task.id, allow_new_case=True)
        assert gathered == refs[2:]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_history_can_recover_once_before_assembling_the_report(runtime_dir):
    db, task, runner, search, _ = await setup_recovery(runtime_dir)
    try:
        runner.agents.update(
            {
                agent: SimpleNamespace()
                for agent in ("fact_investigator", "media_propagation", "history_insight")
            }
        )
        calls = []

        async def run_agent(**kwargs):
            calls.append((kwargs["agent_name"], kwargs["summary_phase"], kwargs["inner_limit"]))

        runner._run_agent = run_agent
        board = await ForumBoard.restore(db, task.id)
        for _ in range(2):
            await runner._recover_report_gaps(
                task.id, task.event_query, board, recovery_round=1, history_only=True
            )

        assert calls == [("history_insight", "quality_recovery", 1)]
        assert not search.calls
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_fetch_tries_next_case_when_top_case_has_no_fetchable_sources(runtime_dir):
    db, task, runner, _, _ = await setup_recovery(runtime_dir)
    try:
        sources, claims, fetched = {}, [], []
        for index, (institution, description, captured) in enumerate(
            (("乙校", "图书馆事件复核", True), ("丙校", "考试处分复核", False))
        ):
            text = f"{institution}{description}，公开材料记载处分已撤销。"
            evidence = await db.add_evidence(
                EvidenceCreate(
                    task_id=task.id,
                    url=f"https://history{index}.example/notice",
                    title=text,
                    snippet=text,
                    fetch_status="fetched" if captured else "discovered",
                    content_text=text if captured else None,
                    fetched_at="2026-01-01" if captured else None,
                    snapshot_path="history.html" if captured else None,
                    content_sha256="history" if captured else None,
                    published_at="2025-04-15",
                    extra={"scope_status": "history", "date_provenance": "search_provider"},
                )
            )
            sources[evidence.local_id] = evidence
            claims.append(
                SimpleNamespace(
                    agent="history_insight",
                    verdict="support",
                    text=text,
                    evidence_ids=[evidence.local_id],
                    analysis_data={
                        "historical_case": {
                            "name": institution + description,
                            "institution": institution,
                            "independent": True,
                            "similarity": description,
                            "difference": "机构与原决定不同",
                        }
                    },
                )
            )

        async def fetch(record, **_kwargs):
            fetched.append(record.local_id)
            return record

        runner._fetch_once = fetch
        runner.agents["history_insight"] = SimpleNamespace()
        await runner._fetch_history_case_leads(task, claims, sources)
        await runner._fetch_history_case_leads(task, claims, sources)
        assert fetched == [claims[1].evidence_ids[0]]
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sealed", [False, True])
async def test_captured_source_date_can_be_recovered_without_refetch_or_claim_changes(
    runtime_dir, sealed
):
    db, task, runner, _, fetcher = await setup_recovery(runtime_dir)
    try:
        raw = (
            "<html><head></head><body><nav>" + "导航" * 18000 + "</nav>"
            '<span id="pubtime_baidu">2025-09-20 09:53:21</span>'
            "<article>甲乙大学图书馆事件调查结果。</article></body></html>"
        )
        snapshot, digest = runner.evidence.snapshots.save(task.id, "captured-original", raw)
        old_fetched_at = "2025-09-20T10:00:00"
        await db.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://school.example.edu/notice/captured",
                title="甲乙大学图书馆事件调查复核通报",
                snippet="甲乙大学发布图书馆事件调查结果。",
                source_name="甲乙大学官网",
                source_role="authority",
                content_text="甲乙大学图书馆事件调查结果。",
                fetch_status="fetched",
                fetched_at=old_fetched_at,
                snapshot_path=snapshot,
                content_sha256=digest,
                extra={"scope_status": "pending", "scope_reasons": ["date_unknown"]},
            )
        )
        if sealed:
            await db.set_task_status(task.id, "done", "finished")
        records = await runner.evidence.refresh_task_publication_metadata(task)
        original = next(e for e in records if e.url.endswith("/captured"))
        assert original.published_at == (None if sealed else "2025-09-20T09:53:21")
        assert original.extra["scope_status"] == ("pending" if sealed else "main")
        assert not fetcher.calls
        assert not await db.list_claims(task.id)
        row = await db.fetch_one("SELECT fetched_at FROM evidence WHERE pk=?", (original.pk,))
        assert row["fetched_at"] == old_fetched_at
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong_hash", "outside_store"])
async def test_metadata_replay_requires_the_original_snapshot_inside_store(runtime_dir, invalid):
    db, task, runner, _, fetcher = await setup_recovery(runtime_dir)
    try:
        store = (
            SnapshotStore(runtime_dir / "outside")
            if invalid == "outside_store"
            else runner.evidence.snapshots
        )
        path, digest = store.save(
            task.id, "original", '<span id="pubtime_baidu">2025-09-20 09:53:21</span>'
        )
        await db.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://school.example.edu/notice/untrusted",
                title="甲乙大学图书馆事件调查通报",
                snippet="调查结果",
                fetch_status="fetched",
                content_text="调查结果",
                snapshot_path=path,
                content_sha256="modified" if invalid == "wrong_hash" else digest,
                fetched_at="2025-09-20",
                extra={"scope_status": "pending", "scope_reasons": ["date_unknown"]},
            )
        )
        records = await runner.evidence.refresh_task_publication_metadata(task)
        original = next(e for e in records if e.url.endswith("/untrusted"))
        assert original.published_at is None
        assert original.extra["scope_status"] == "pending"
        assert not fetcher.calls
    finally:
        await db.close()
