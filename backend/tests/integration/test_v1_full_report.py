import asyncio
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from yuqing.services.forum import ForumBoard, ForumMessageCreate
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.report_delivery import EvidencePackageBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate
from yuqing.storage.snapshots import SnapshotStore


class InstitutionReviewer:
    def bind(self, *args):
        return self

    async def review(self, texts, *, kind):
        from yuqing.services.institution_scope import ScopeReview

        return [
            ScopeReview("rejected" if "张三" in text else "accepted", "policy", text)
            for text in texts
        ]

    async def accepted(self, texts, *, kind):
        return ["张三" not in text for text in texts]


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_quote", [False, True])
async def test_comment_priority_drops_theme_removed_by_final_privacy_review(
    runtime_dir: Path, reject_quote: bool
):
    from yuqing.services.institution_scope import ScopeReview

    class Reviewer(InstitutionReviewer):
        async def review(self, texts, *, kind):
            return [
                ScopeReview("rejected", "policy", text)
                if reject_quote and text == "希望解释复核依据"
                else ScopeReview("incomplete", "local_budget", text)
                if not reject_quote and "主题综合原话" in text
                else ScopeReview("accepted", "policy", text)
                for text in texts
            ]

    database = Database(runtime_dir / "comment-priority-privacy.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(
                event_query="机构复核事项", comment_mode="smart", investigation_scope="public_event"
            )
        )
        evidence = await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.test/notice",
                title="公开通报",
                snippet="机构发布复核结果。",
            )
        )
        theme = {
            "title": "解释复核依据",
            "text": "主题综合原话仍需最终隐私审查",
            "interpretation": "主题综合原话仍需最终隐私审查",
            **{
                key: "样本希望了解复核依据。"
                for key in (
                    "stance_analysis",
                    "controversy",
                    "risk_assessment",
                    "response_gap",
                    "response_action",
                    "priority_reason",
                    "uncertainty",
                )
            },
            "priority": "补充说明",
            "comment_refs": ["M1"],
            "quotes": [
                {
                    "id": "M1",
                    "text": "希望解释复核依据",
                    "platform": "weibo",
                    "evidence_ref": evidence.local_id,
                }
            ],
            "sample_count": 1,
            "stance_counts": {"质疑": 1},
            "platform_counts": {"weibo": 1},
            "time_counts": {"时间未知": 1},
            "review_status": "accepted",
            "evidence_refs": [evidence.local_id],
        }
        board = await ForumBoard.restore(database, task.id)
        await board.post(
            ForumMessageCreate(
                task_id=task.id,
                round=1,
                agent="comment_insight",
                type="summary",
                content="已形成待最终审查的样本主题。",
                payload={
                    "comment_analysis": {
                        "version": 4,
                        "status": "complete",
                        "items": [theme],
                        "samples": [
                            {
                                "id": "M1",
                                "text": "希望解释复核依据",
                                "platform": "weibo",
                                "evidence_ref": evidence.local_id,
                            }
                        ],
                        "coverage": {
                            "classified": 1,
                            "irrelevant": 0,
                            "unclassified": 0,
                            "reviewed_themes": 1,
                            "in_reviewed_themes": 1,
                            "relevant_without_reviewed_theme": 0,
                        },
                        "priority_order": [
                            {
                                "title": theme["title"],
                                "priority": theme["priority"],
                                "reason": theme["priority_reason"],
                                "sample_count": 1,
                                "comment_refs": ["M1"],
                            }
                        ],
                    }
                },
            )
        )
        _, report, _ = await FullReportBuilder(
            database, runtime_dir / "reports", scope_reviewer=Reviewer()
        ).build(task.id, forum=board.history())
        block = next(b for b in report["blocks"] if b["type"] == "comment_insight")
        assert block["items"] == []
        assert block["priority_order"] == []
        assert block["coverage"]["reviewed_themes"] == 0
        assert report["quality"]["chapter_status"]["comments"]["status"] == "failed"
        if reject_quote:
            assert block["samples"] == []
            assert block["coverage"]["final_scope_hidden_samples"] == 1
            assert block["coverage"]["displayed_samples"] == 0
            assert block["coverage"]["classified"] == 1
            from yuqing.render.html import render_html

            assert "希望解释复核依据" not in render_html(report)
        else:
            assert len(block["samples"]) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reviewed_comment_themes_keep_incomplete_privacy_coverage_partial(runtime_dir: Path):
    database = Database(runtime_dir / "partial-comment-privacy.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(event_query="机构公开复核", comment_mode="smart")
        )
        evidence = await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.test/notice",
                title="机构复核通报",
                snippet="机构已公开复核结果。",
            )
        )
        board = await ForumBoard.restore(database, task.id)
        await board.post(
            ForumMessageCreate(
                task_id=task.id,
                round=1,
                agent="comment_insight",
                type="summary",
                content="已审样本希望解释复核依据，另有样本隐私审查尚未完成。",
                refs=[evidence.local_id],
                payload={
                    "comment_analysis": {
                        "version": 1,
                        "status": "complete",
                        "items": [
                            {
                                "text": "希望解释复核依据",
                                "evidence_refs": [evidence.local_id],
                                "comment_refs": ["M1"],
                                "sample_count": 1,
                                "review_status": "accepted",
                                "platform_counts": {"weibo": 1},
                            }
                        ],
                        "samples": [
                            {
                                "id": "M1",
                                "text": "希望解释复核依据",
                                "platform": "weibo",
                                "evidence_ref": evidence.local_id,
                            }
                        ],
                        "coverage": {
                            "classified": 1,
                            "unclassified": 0,
                            "relevant_without_reviewed_theme": 0,
                            "scope_review_incomplete": 399,
                        },
                    }
                },
            )
        )
        _, report, _ = await FullReportBuilder(database, runtime_dir / "reports").build(
            task.id, forum=board.history()
        )
        comments = report["quality"]["chapter_status"]["comments"]
        assert comments["status"] == "partial"
        assert comments["coverage"]["scope_review_incomplete"] == 399
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_institution_report_hides_personal_source_text_and_preserves_link(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "institution.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="某高校图书馆事件", investigation_scope="institution")
    )
    snapshot = runtime_dir / "source.html"
    snapshot.write_text("张三相关网页原文", encoding="utf-8")
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.com/notice",
            title="学校回应张三相关争议",
            snippet="学校回应张三相关争议，并公布复核安排。",
            source_name="某高校",
            source_role="party",
            source_tier=1,
        )
    )
    await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="学校公布了复核安排。",
            agent="fact_investigator",
            section="fact_check",
            evidence_ids=[evidence.local_id],
        ),
        **claim_limits,
    )

    report_id, report, path = await FullReportBuilder(
        database, runtime_dir / "reports", scope_reviewer=InstitutionReviewer()
    ).build(task.id)
    html = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")

    assert "张三" not in json.dumps(report, ensure_ascii=False)
    assert "张三" not in html
    assert "学校公布了复核安排" in html
    assert "https://example.com/notice" in html
    appendix = next(block for block in report["blocks"] if block["type"] == "evidence_appendix")
    assert appendix["items"][0]["snapshot_pk"] is None
    # A legacy or manually migrated IR may still hold a snapshot reference.
    await database.update_evidence_fetched(
        task.id,
        evidence.local_id,
        content_text="张三相关网页原文",
        snapshot_path=str(snapshot),
        content_sha256="a" * 64,
    )
    appendix["items"][0]["fetch_status"] = "fetched"
    appendix["items"][0]["snapshot_pk"] = evidence.pk
    report["task"].pop("investigation_scope", None)
    await database.save_report(task.id, report_id, report, path, report["metrics"])
    packaged = await EvidencePackageBuilder(database, SnapshotStore(runtime_dir)).build(report_id)
    with zipfile.ZipFile(BytesIO(packaged)) as archive:
        assert archive.namelist() == ["report.html", "manifest.json"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["evidence"][0]["url"] == "https://example.com/notice"
        assert manifest["evidence"][0]["snapshot_file"] is None
        assert "张三" not in archive.read("report.html").decode("utf-8")
    await database.close()


@pytest.mark.asyncio
async def test_full_report_has_ten_sections_real_charts_and_offline_interactions(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "report.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="完整专报测试"))
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://www.gov.cn/notice",
            title="主管部门公开通报",
            snippet="主管部门于 2026 年 8 月 12 日发布通报。",
            source_name="主管部门",
            publisher_entity="主管部门",
            source_role="authority",
            source_tier=1,
            published_at="2026-08-12T09:00:00+08:00",
            provider="fixture",
        )
    )
    await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://news.example.com/context",
            title="背景材料",
            snippet="这条材料用于背景统计，未直接绑定重要 claim。",
            source_name="背景来源",
            publisher_entity="背景来源",
            source_role="independent",
            source_tier=3,
            published_at="2026-08-10T09:00:00+08:00",
            provider="fixture",
        )
    )
    await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://another.example.com/context",
            title="另一背景材料",
            snippet="第三条材料只用于验证真实图表口径。",
            source_name="另一来源",
            publisher_entity="另一来源",
            source_role="independent",
            source_tier=3,
            published_at="2026-08-11T09:00:00+08:00",
            provider="fixture",
        )
    )
    await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="主管部门于 2026 年 8 月 12 日发布通报。",
            agent="fact_investigator",
            section="fact_check",
            evidence_ids=[evidence.local_id],
        ),
        **claim_limits,
    )
    board = await ForumBoard.restore(database, task.id)
    await board.post(
        ForumMessageCreate(
            task_id=task.id,
            round=1,
            agent="media_propagation",
            type="summary",
            content="目前仅取得一条权威来源，传播结构样本不足。",
            refs=[evidence.local_id],
        )
    )

    report_id, report, html_path = await FullReportBuilder(database, runtime_dir / "reports").build(
        task.id, forum=board.history(), orchestration_limitations=[]
    )

    sections = {block["section"] for block in report["blocks"]}
    charts = [block for block in report["blocks"] if block["type"] == "chart"]
    html = await asyncio.to_thread(Path(html_path).read_text, encoding="utf-8")
    assert report_id
    assert sections == {f"{number:02d}" for number in range(10)}
    assert len(charts) == 3
    assert {block["chart_kind"] for block in charts} == {"funnel", "matrix", "timeline"}
    assert {
        item["label"]
        for item in next(
            block for block in report["blocks"] if block.get("block_id") == "b_00_kpi"
        )["items"]
    } == {
        "已证实陈述",
        "待核验陈述",
        "来源直接支持",
        "已取得原文",
        "实际引用材料",
    }
    assert any(block["type"] == "data_quality" for block in report["blocks"])
    assert 'data-view="brief"' in html
    assert "data-badge-filter" in html
    assert 'class="report-toc"' in html
    assert 'aria-current="location"' in html
    assert ":hover" in html
    assert ":focus-visible" in html
    assert "IntersectionObserver" in html
    assert "搜索摘要（非原文）" in html
    assert '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,' in html
    assert "https://cdn." not in html
    assert report["metrics"]["key_claims_candidate"] == 1
    assert report["metrics"]["citation_coverage"] == 1.0
    assert report["metrics"]["evidence_total"] == 3
    assert report["metrics"]["independent_publishers"] == 3
    assert report["metrics"]["time_span_days"] == 2
    funnel = next(
        block for block in report["blocks"] if block.get("block_id") == "b_04_evidence_funnel"
    )
    assert funnel["title"] == "证据获取漏斗"
    assert funnel["items"][0] == {"label": "去重检索材料", "value": 3}
    matrix = next(
        block for block in report["blocks"] if block.get("block_id") == "b_04_verification_matrix"
    )
    assert matrix["items"][0]["claim_ref"] == "C001"
    await database.close()


@pytest.mark.asyncio
async def test_publication_node_drops_evidence_rejected_by_claim_verification(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "media-rejected-citation.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="某机构发布调查通报"))
    first = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.cn/first",
            title="机构调查通报",
            snippet="机构发布了调查通报。",
            published_at="2026-08-10T10:00:00+08:00",
        )
    )
    second = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.cn/second",
            title="媒体后续报道",
            snippet="媒体刊发后续报道。",
            published_at="2026-08-11T10:00:00+08:00",
        )
    )
    claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="机构发布调查通报，媒体刊发后续报道。",
            agent="media_propagation",
            section="propagation",
            evidence_ids=[first.local_id, second.local_id],
            quotes=[
                QuoteCreate(evidence_id=first.local_id, quote_type="paraphrase"),
                QuoteCreate(evidence_id=second.local_id, quote_type="paraphrase"),
            ],
            analysis_data={
                "publication_node": {
                    "evidence_id": second.local_id,
                    "publisher": "媒体",
                    "published_at": "2026-08-11T10:00:00+08:00",
                    "node_type": "independent",
                    "framing": "后续报道",
                },
                "propagation_edges": [
                    {
                        "from_evidence_id": first.local_id,
                        "to_evidence_id": second.local_id,
                        "relation": "follow_up",
                    }
                ],
            },
        ),
        **claim_limits,
    )
    await database.set_evidence_relation(
        claim.pk,
        first.pk,
        relation="partial",
        reason="支持通报部分",
        cited_sentence=first.snippet or "",
        cited_verified=True,
    )
    await database.set_evidence_relation(
        claim.pk,
        second.pk,
        relation="not_mentioned",
        reason="不支持该陈述",
        cited_sentence=second.snippet or "",
        cited_verified=False,
    )

    _, report, _ = await FullReportBuilder(database, runtime_dir / "reports").build(task.id)

    network = next(
        block for block in report["blocks"] if block["block_id"] == "b_04_publication_network"
    )
    assert network["nodes"] == []
    assert network["edges"] == []
    await database.close()


@pytest.mark.asyncio
async def test_reviewed_relation_can_add_dated_publication_page_without_media_claim(
    runtime_dir: Path,
):
    class Reporter:
        relation_diagnostics: list[dict] = []
        relation_candidates: list[dict] = []

        async def recover_relations(self, sources: list[dict]) -> list[dict]:
            assert {item["evidence_ref"] for item in sources} == {"E001", "E002"}
            return [
                {
                    "from_evidence_id": "E001",
                    "to_evidence_id": "E002",
                    "relation": "repost",
                    "support_evidence_id": "E002",
                    "quote": "来源：甲方",
                    "evidence_refs": ["E002"],
                    "review_status": "accepted",
                }
            ]

        async def enrich(self, context: dict) -> dict:
            return {"analyses": [], "summary_claim_refs": []}

    database = Database(runtime_dir / "reviewed-relation.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="甲方发布某机构通报"))
    for url, title, publisher, body in (
        ("https://one.example.cn/news", "甲方原始报道", "甲方", "甲方发布某机构通报。"),
        (
            "https://two.example.cn/news",
            "乙方转载甲方报道",
            "乙方",
            "来源：甲方。甲方发布某机构通报。",
        ),
    ):
        await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url=url,
                title=title,
                snippet=body,
                publisher_entity=publisher,
                published_at="2025-09-20T10:00:00+08:00",
                fetch_status="fetched",
                fetched_at="2025-09-20T11:00:00+08:00",
                content_text=body,
                snapshot_path=str(runtime_dir / "source.html"),
                content_sha256="a" * 64,
                extra={"scope_status": "main", "date_provenance": "page_metadata"},
            )
        )

    _, report, _ = await FullReportBuilder(
        database, runtime_dir / "reviewed-relation-reports", reporter=Reporter()
    ).build(task.id)
    network = next(
        block for block in report["blocks"] if block["block_id"] == "b_04_publication_network"
    )
    assert {node["evidence_id"] for node in network["nodes"]} == {"E001", "E002"}
    assert len(network["edges"]) == 1
    assert network["edges"][0]["review_status"] == "accepted"
    assert network["fallback_text"] is None
    await database.close()


@pytest.mark.asyncio
async def test_report_timeline_excludes_out_of_window_background_material(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "timeline-scope.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="武汉大学具体事件",
            time_range={"from": "2023-01-01", "to": "2026-01-01"},
        )
    )
    inside = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://www.whu.edu.cn/inside",
            title="范围内校方通报",
            snippet="2025 年 9 月 20 日校方发布通报。",
            published_at="2025-09-20T10:00:00+08:00",
            extra={"scope_status": "main", "date_provenance": "page_visible"},
        )
    )
    outside = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://www.whu.edu.cn/outside",
            title="范围外后续材料",
            snippet="2026 年 6 月 4 日发布后续材料。",
            published_at="2026-06-04T10:00:00+08:00",
            extra={"scope_status": "background", "date_provenance": "page_visible"},
        )
    )
    inside_claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="2025 年 9 月 20 日校方发布通报。",
            agent="fact_investigator",
            evidence_ids=[inside.local_id],
        ),
        **claim_limits,
    )
    outside_claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="2026 年 6 月 4 日发布后续材料。",
            agent="fact_investigator",
            evidence_ids=[outside.local_id],
        ),
        **claim_limits,
    )
    for claim, evidence in ((inside_claim, inside), (outside_claim, outside)):
        await database.set_evidence_relation(
            claim.pk,
            evidence.pk,
            relation="support",
            reason="fixture",
            cited_sentence=evidence.snippet or "",
            cited_verified=True,
        )

    _, report, _ = await FullReportBuilder(database, runtime_dir / "reports").build(task.id)

    timeline = next(
        block for block in report["blocks"] if block["block_id"] == "b_02_correction_timeline"
    )
    assert [item["date"][:10] for item in timeline["items"]] == ["2025-09-20"]
    assert all("范围外后续材料" not in item["text"] for item in timeline["items"])
    appendix = next(block for block in report["blocks"] if block["type"] == "evidence_appendix")
    outside_item = next(item for item in appendix["items"] if item["evidence_ref"] == "E002")
    assert outside_item["scope_status"] == "background"
    await database.close()
