import asyncio
from pathlib import Path

import pytest

from yuqing.services.full_report import FullReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, TaskCreate


@pytest.mark.asyncio
@pytest.mark.parametrize("date_confirmed", [True, False])
async def test_unresolved_independent_case_is_formal_or_date_uncertain_reference(
    runtime_dir, claim_limits, date_confirmed
):
    database = Database(runtime_dir / "cases.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="本品牌制动故障"))
        evidence = await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.cn/earlier-case",
                title="另一品牌启动召回调查",
                snippet="另一品牌启动召回调查，处理仍在进行。",
                source_name="监管部门",
                source_tier=1,
                source_role="authority",
                published_at="2020-01-01T00:00:00+08:00",
                extra={
                    "scope_status": "history",
                    "date_provenance": "page_visible" if date_confirmed else "search_provider",
                },
            )
        )
        claim = await database.add_claim(
            ClaimCreate(
                task_id=task.id,
                text="另一品牌启动召回调查。",
                agent="history_insight",
                section="history",
                evidence_ids=[evidence.local_id],
                analysis_data={
                    "historical_case": {
                        "name": "另一品牌制动召回事件",
                        "institution": "另一品牌",
                        "independent": True,
                        "similarity": "安全缺陷与召回机制",
                        "difference": "品牌与缺陷部件不同",
                    }
                },
            ),
            **claim_limits,
        )
        await database.set_evidence_relation(
            claim.pk,
            evidence.pk,
            relation="support",
            reason="原文记载",
            cited_sentence="",
            cited_verified=False,
        )
        await database.set_claim_verification(
            claim.pk,
            badge="unverified",
            verdict="support",
            reason="单一来源",
            state="complete",
            independent_sources=1,
            max_source_tier=1,
            verifier_model="fixture",
        )
        _, report, path = await FullReportBuilder(database, runtime_dir / "reports").build(task.id)
        history = next(block for block in report["blocks"] if block["type"] == "history_compare")
        if date_confirmed:
            assert len(history["cards"]) == 1
            assert "未见最终处理结果" in history["cards"][0]["outcome"]
            assert not history["reference_candidates"]
        else:
            assert not history["cards"]
            assert len(history["reference_candidates"]) == 1
            page = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            assert "待核实参考案例（不计入正式历史对照）" in page
            assert "不作为历史结论或建议依据" in page
        facts = next(block for block in report["blocks"] if block["type"] == "fact_check_table")
        assert not facts["items"]
    finally:
        await database.close()
