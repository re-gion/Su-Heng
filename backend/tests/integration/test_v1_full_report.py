import asyncio
from pathlib import Path

import pytest

from yuqing.services.forum import ForumBoard, ForumMessageCreate
from yuqing.services.full_report import FullReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, TaskCreate


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
    assert '<link rel="icon" href="data:,">' in html
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
