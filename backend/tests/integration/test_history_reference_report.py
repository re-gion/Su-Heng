import asyncio
import copy
from pathlib import Path

import pytest

from yuqing.render.validator import ReportValidationError, validate_report
from yuqing.services.full_report import FullReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, TaskCreate


@pytest.mark.asyncio
@pytest.mark.parametrize("date_confirmed", [True, False])
@pytest.mark.parametrize("has_background", [True, False])
async def test_unresolved_independent_case_is_formal_or_date_uncertain_reference(
    runtime_dir, claim_limits, date_confirmed, has_background
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
                snippet="另一品牌因制动故障启动召回调查，处理仍在进行。",
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
                text="另一品牌因制动故障启动召回调查。",
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
                        "background": {
                            "subject": "另一品牌",
                            "event": "制动故障",
                            "progress": "启动召回调查",
                        }
                        if has_background
                        else {},
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
        seen_history = []

        class Reporter:
            async def enrich(self, context):
                seen_history.extend(
                    fact
                    for fact in context["facts"]
                    if fact.get("origin_agent") == "history_insight"
                )
                return {}

        _, report, path = await FullReportBuilder(
            database, runtime_dir / "reports", reporter=Reporter()
        ).build(task.id)
        assert bool(seen_history) == (date_confirmed and has_background)
        history = next(block for block in report["blocks"] if block["type"] == "history_compare")
        if date_confirmed and has_background:
            assert len(history["cards"]) == 1
            assert "未记载最终处理结果" in history["cards"][0]["outcome"]
            assert not history["reference_candidates"]
            tampered = copy.deepcopy(report)
            changed = next(b for b in tampered["blocks"] if b["type"] == "history_compare")
            changed["cards"][0]["background_facts"][0]["text"] += "监管已宣布结案。"
            with pytest.raises(ReportValidationError, match="简介未从对应获支持陈述回填"):
                validate_report(tampered)
        else:
            assert not history["cards"]
            assert len(history["reference_candidates"]) == 1
            page = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            assert "待核实参考案例（不计入正式历史对照）" in page
            if date_confirmed:
                assert "案例背景待补充" in page
                assert "不作为正式历史对照或建议依据" in page
            else:
                assert "不作为历史结论或建议依据" in page
        facts = next(block for block in report["blocks"] if block["type"] == "fact_check_table")
        assert not facts["items"]
    finally:
        await database.close()
