import asyncio
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuqing.render.ir_migrations import migrate_report
from yuqing.render.validator import ReportValidationError, validate_report
from yuqing.services.full_report import FullReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate


@pytest.fixture
async def analytical_report(runtime_dir, claim_limits):
    db = Database(runtime_dir / "analysis.db")
    await db.initialize()
    task = await db.create_task(
        TaskCreate(
            event_query="高校诉求受理", time_range={"from": "2026-09-01", "to": "2026-09-20"}
        )
    )
    text = "校方通报本次收到有效诉求120条，已交由工作组核查。"
    evidence = await db.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.edu/notice",
            title="校方诉求通报",
            snippet=text,
            source_name="校方",
            source_role="party",
            published_at="2026-09-10T10:00:00+08:00",
            fetch_status="fetched",
            content_text=text,
            fetched_at="2026-09-01T12:00:00+08:00",
            snapshot_path="fixture.html",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extra={
                "scope_status": "main",
                "main_eligible": True,
                "date_provenance": "page_visible",
            },
        )
    )
    claim = await db.add_claim(
        ClaimCreate(
            task_id=task.id,
            agent="fact_investigator",
            text=text,
            evidence_ids=[evidence.local_id],
            quotes=[QuoteCreate(evidence_id=evidence.local_id, quote=text, quote_type="verbatim")],
        ),
        **claim_limits,
    )
    await db.set_claim_verification(
        claim.pk,
        badge="verified",
        verdict="support",
        reason="fixture",
        state="complete",
        independent_sources=1,
        max_source_tier=2,
        verifier_model="fixture",
    )
    await db.add_claim(
        ClaimCreate(
            task_id=task.id,
            agent="fact_investigator",
            text="校方通报本次所有诉求均已解决。",
            evidence_ids=[evidence.local_id],
        ),
        **claim_limits,
    )

    class Reporter:
        async def enrich(self, context):
            assert context["task"]["time_range_from"] == "2026-09-01"
            assert [item["claim_ref"] for item in context["facts"]] == ["C001"]
            assert context["sources"][0]["excerpt"] == text
            common = {
                "claim_refs": ["C001"],
                "implication": "需要区分受理与处理结果。",
                "uncertainty": "尚无材料证明事项均已解决。",
                "owner": "诉求受理部门",
                "trigger": "核查形成新进展时",
                "action": "公开受理步骤和反馈渠道",
            }
            return {
                "summary_claim_refs": ["C001"],
                "analyses": [
                    {
                        **common,
                        "section": "04",
                        "title": "受理口径与进展口径",
                        "interpretation": "若校方尚在核查，受理数量不能解释为处理完成数量。",
                    },
                    {
                        **common,
                        "section": "07",
                        "title": "建立进度反馈",
                        "interpretation": "在结果未明时，说明流程可能帮助相关人员理解处理状态。",
                    },
                    {
                        **common,
                        "section": "05",
                        "claim_refs": ["C002"],
                        "title": "未经核验的结案判断",
                        "interpretation": "所有诉求均已解决。",
                    },
                ],
                "measurements": [
                    {
                        "evidence_ref": "E001",
                        "claim_refs": ["C001"],
                        "value_text": "120条",
                        "label": "有效诉求",
                        "quote": text,
                    }
                ],
            }

    _, report, path = await FullReportBuilder(
        db, runtime_dir / "reports", reporter=Reporter()
    ).build(task.id, orchestration_limitations=["有限采样", "有限采样"])
    await db.close()
    return report, await asyncio.to_thread(Path(path).read_text, encoding="utf-8")


@pytest.mark.asyncio
async def test_full_report_has_grounded_analysis_and_compact_audit(analytical_report):
    report, rendered = analytical_report
    assert report["schema_version"] == "0.9"
    assert report["quality"]["status"] == "analysis_available"
    assert report["quality"]["sourced_measurements"] == 1
    assert report["quality"]["rejected_items"]["analysis_reference"] == 1
    assert "已形成有依据的分析条目" in rendered
    assert "高校诉求受理" in rendered
    assert "120条" in rendered
    assert "优先回看证据卡中的原文关键句" not in rendered
    assert rendered.count("有限采样") == 1
    assert 'class="audit-module"' in rendered
    assert 'class="analysis-basis"' in rendered
    assert "优先决策事项（分析判断）" in rendered
    assert 'href="#b_07_analysis"' in rendered
    assert validate_report(report).errors == []


@pytest.mark.asyncio
async def test_scope_redaction_preserves_canonical_analysis_observation(
    analytical_report, runtime_dir
):
    report = copy.deepcopy(analytical_report[0])
    report["blocks"] = [b for b in report["blocks"] if b["type"] != "metric_cards"]
    analysis = next(b for b in report["blocks"] if b["type"] == "analysis")["items"][0]
    analysis["interpretation"] += "后续需要工作组说明进度。"
    fact = next(b for b in report["blocks"] if b["type"] == "fact_check_table")["items"][0]
    original_fact = copy.deepcopy(fact)

    class Reviewer:
        def bind(self, *_args):
            pass

        async def review(self, texts, *, kind):
            assert kind == "report_text"
            return [
                SimpleNamespace(
                    allowed=True,
                    status="accepted",
                    text=text.replace("工作组", "有关部门"),
                    reason="privacy_redacted",
                    diagnostic=None,
                )
                for text in texts
            ]

    builder = FullReportBuilder(None, runtime_dir, scope_reviewer=Reviewer())
    task = SimpleNamespace(id="fixture", investigation_scope="public_event")
    await builder._retain_reviewed_blocks(report, task)
    assert fact == original_fact
    analyses = [b for b in report["blocks"] if b["type"] == "analysis"]
    assert analyses
    assert validate_report(report).errors == []
    assert analysis["interpretation"].endswith("后续需要有关部门说明进度。")
    for block in analyses:
        for item in block["items"]:
            assert item["observation"] == original_fact["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption,rule",
    [
        ("observation", "R19"),
        ("refs", "R2"),
        ("measurement", "R20"),
        ("number", "R20"),
        ("substring", "R20"),
        ("unit_removed", "R20"),
    ],
)
async def test_replay_contract_rejects_tampered_analysis_and_metrics(
    analytical_report, corruption, rule
):
    report = copy.deepcopy(analytical_report[0])
    analysis = next(b for b in report["blocks"] if b["type"] == "analysis")["items"][0]
    metric = next(b for b in report["blocks"] if b["type"] == "metric_cards")["items"][0]
    if corruption == "observation":
        analysis["observation"] = "所有事项都已处理完毕。"
    elif corruption == "refs":
        analysis["claim_refs"] = ["C099"]
    elif corruption == "measurement":
        metric["quote"] = "全网讨论高达120条。"
    elif corruption == "substring":
        metric["value"] = "20条"
    elif corruption == "unit_removed":
        metric["value"] = "120"
    else:
        metric["value"] = "120万条"
    with pytest.raises(ReportValidationError, match=rule):
        validate_report(report)


def test_v04_migration_preserves_authority_and_is_idempotent():
    original = {
        "schema_version": "0.4",
        "min_reader_minor": 4,
        "blocks": [
            {
                "type": "fact_check_table",
                "items": [{"claim_ref": "C001", "badge": "unverified", "text": "原始陈述"}],
            }
        ],
    }
    migrated = migrate_report(original)
    assert migrated["blocks"] == original["blocks"]
    assert migrated["schema_version"] == "0.9"
    assert migrate_report(migrated) == migrated
    assert original["schema_version"] == "0.4"
