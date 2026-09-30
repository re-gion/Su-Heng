import hashlib
from datetime import date, timedelta

import pytest

from yuqing.services.full_report import FullReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate


async def add_fact(db, task, text, *, history=False, date="2025-09-20", scope_status=None):
    evidence = await db.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.edu/" + ("history" if history else f"notice-{date}"),
            title=text,
            snippet=text,
            source_name="机构通报",
            source_role="party",
            published_at=date,
            fetch_status="fetched",
            content_text=text,
            fetched_at="2026-09-24",
            snapshot_path="fixture.html",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extra={
                "scope_status": scope_status or ("history" if history else "main"),
                "date_provenance": "page_visible",
            },
        )
    )
    claim = await db.add_claim(
        ClaimCreate(
            task_id=task.id,
            agent="history_insight" if history else "fact_investigator",
            text=text,
            section="history" if history else "fact_check",
            evidence_ids=[evidence.local_id],
            analysis_data={
                "historical_case": {
                    "name": "乙校复核案",
                    "institution": "乙校",
                    "independent": True,
                    "similarity": "处分后的复核与更正",
                    "difference": "不同机构和事实",
                    "outcome": "已公开更正",
                }
            }
            if history
            else {},
            quotes=[QuoteCreate(evidence_id=evidence.local_id, quote=text, quote_type="verbatim")],
        ),
        max_claims=100,
        max_evidence_per_claim=5,
    )
    await db.set_claim_verification(
        claim.pk,
        badge="unverified",
        verdict="support",
        reason="单方来源",
        state="complete",
        independent_sources=1,
        max_source_tier=2,
        verifier_model="fixture",
    )
    await db.execute_write(
        "UPDATE claim_evidence SET relation='support' WHERE claim_pk=?", (claim.pk,)
    )
    return claim


@pytest.mark.asyncio
async def test_unverified_diagnostic_keeps_attributed_summary(runtime_dir):
    db = Database(runtime_dir / "diagnostic.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(
                event_query="甲校调查复核", time_range={"from": "2025-01-01", "to": "2025-12-31"}
            )
        )
        await add_fact(db, task, "甲校通报称已撤销原处分。")
        _, report, _ = await FullReportBuilder(db, runtime_dir / "reports").build(
            task.id, diagnostic_only=True
        )
        summary = next(b for b in report["blocks"] if b["type"] == "executive_summary")
        assert summary["what"][0]["text"] == "甲校通报称已撤销原处分。"
        facts = next(b for b in report["blocks"] if b["type"] == "fact_check_table")
        assert facts["items"][0]["badge"] == "unverified"
        assert report["quality"]["release_label"] == "retrieval_diagnostic"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_event_later_result_stays_in_report_outside_focus_window(runtime_dir):
    db = Database(runtime_dir / "event-context.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(
                event_query="甲校调查复核", time_range={"from": "2025-07-01", "to": "2025-07-31"}
            )
        )
        await add_fact(db, task, "2025年7月20日甲校通报已启动复核。", date="2025-07-20")
        later = await add_fact(
            db,
            task,
            "2025年9月20日甲校通报复核结果。",
            date="2025-09-20",
            scope_status="event_context",
        )
        _, report, _ = await FullReportBuilder(db, runtime_dir / "reports").build(task.id)
        fact_table = next(
            block for block in report["blocks"] if block["type"] == "fact_check_table"
        )
        assert later.local_id in {item["claim_ref"] for item in fact_table["items"]}
        timeline = next(
            block for block in report["blocks"] if block["block_id"] == "b_02_correction_timeline"
        )
        later_node = next(
            item for item in timeline["items"] if later.local_id in item["claim_refs"]
        )
        assert later_node["window_label"] == "重点窗口后的本事件进展"
        assert report["metrics"]["event_context_evidence_total"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "date_kind,expected", [("prior", True), ("after_window", True), ("future", False)]
)
async def test_history_context_is_separate_and_uses_task_start_as_publication_cutoff(
    runtime_dir, date_kind, expected
):
    db = Database(runtime_dir / "history.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(
                event_query="甲校调查复核", time_range={"from": "2025-01-01", "to": "2025-12-31"}
            )
        )
        historical_date = {
            "prior": "2023-06-01",
            "after_window": "2026-09-01",
            "future": (date.fromisoformat(task.created_at[:10]) + timedelta(days=1)).isoformat(),
        }[date_kind]
        await add_fact(db, task, "甲校通报称已撤销原处分。")
        await add_fact(
            db, task, "乙校在复核后公开更正先前处理决定。", history=True, date=historical_date
        )

        class Reporter:
            async def enrich(self, context):
                historical = [f for f in context["facts"] if f["origin_agent"] == "history_insight"]
                assert bool(historical) == expected
                return {}

        _, report, _ = await FullReportBuilder(
            db, runtime_dir / "reports", reporter=Reporter()
        ).build(task.id)
        main = next(b for b in report["blocks"] if b["type"] == "fact_check_table")
        assert all(f["origin_agent"] != "history_insight" for f in main["items"])
        assert any(b["type"] == "historical_facts" for b in report["blocks"]) == expected
        if not expected:
            history = next(b for b in report["blocks"] if b["type"] == "history_compare")
            assert history["excluded_candidates"][0]["evidence_refs"]
            assert "晚于" in history["excluded_candidates"][0]["reason"]
        summary = next(b for b in report["blocks"] if b["type"] == "executive_summary")
        assert all("乙校" not in item["text"] for item in summary["what"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_claim_can_gain_new_source_and_requires_reverification(runtime_dir):
    db = Database(runtime_dir / "corroboration.db")
    await db.initialize()
    try:
        task = await db.create_task(TaskCreate(event_query="甲校处分争议"))
        for url in ("first", "second"):
            await db.add_evidence(
                EvidenceCreate(
                    task_id=task.id,
                    url=f"https://example.edu/{url}",
                    title="甲校发布处分情况",
                    snippet="甲校通报称将复核处分。",
                    source_name="机构通报",
                    fetch_status="fetched",
                    content_text="甲校通报称将复核处分。",
                    fetched_at="2026-09-24",
                    snapshot_path=f"{url}.html",
                    content_sha256=hashlib.sha256(url.encode()).hexdigest(),
                )
            )
        first = await db.add_claim(
            ClaimCreate(
                task_id=task.id,
                agent="fact_investigator",
                text="甲校通报称将复核处分。",
                evidence_ids=["E001"],
            ),
            max_claims=10,
            max_evidence_per_claim=3,
        )
        await db.set_claim_verification(
            first.pk,
            badge="unverified",
            verdict="support",
            reason="单源",
            state="complete",
            independent_sources=1,
            max_source_tier=2,
            verifier_model="fixture",
        )
        updated = await db.add_claim(
            ClaimCreate(
                task_id=task.id,
                agent="fact_investigator",
                text="甲校通报称将复核处分。",
                evidence_ids=["E002"],
            ),
            max_claims=10,
            max_evidence_per_claim=3,
        )
        assert updated.local_id == first.local_id
        assert updated.evidence_ids == ["E001", "E002"]
        assert updated.verification_state == "pending"
        assert updated.badge is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_low_tier_supported_history_stays_unverified_but_available_for_comparison(
    runtime_dir,
):
    db = Database(runtime_dir / "conditional-history.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(
                event_query="甲校调查复核", time_range={"from": "2025-01-01", "to": "2025-12-31"}
            )
        )
        await add_fact(db, task, "甲校通报称已撤销原处分。")
        historical = await add_fact(
            db,
            task,
            "乙校在复核后公开更正先前处理决定。",
            history=True,
            date="2023-06-01",
        )
        await db.set_claim_verification(
            historical.pk,
            badge="unverified",
            verdict="not_mentioned",
            reason="来源角色不足以独立核实",
            state="complete",
            independent_sources=0,
            max_source_tier=4,
            verifier_model="fixture",
        )
        seen = []

        class Reporter:
            async def enrich(self, context):
                seen.extend(f for f in context["facts"] if f["origin_agent"] == "history_insight")
                return {}

        _, report, _ = await FullReportBuilder(
            db, runtime_dir / "reports", reporter=Reporter()
        ).build(task.id)
        assert seen and seen[0]["badge"] == "unverified"
        basis = next(b for b in report["blocks"] if b["type"] == "historical_facts")
        assert basis["items"][0]["badge"] == "unverified"
        comparison = next(b for b in report["blocks"] if b["type"] == "history_compare")
        assert comparison["cards"][0]["claim_ref"] == historical.local_id
        assert comparison["cards"][0]["evidence_refs"] == ["E002"]
        assert "独立核实不足" in comparison["cards"][0]["summary"]
        assert "相似机制" in comparison["cards"][0]["comparison"]
    finally:
        await db.close()
