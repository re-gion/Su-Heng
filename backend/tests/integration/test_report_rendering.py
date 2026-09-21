import copy
import json
from pathlib import Path

import pytest

from yuqing.render.html import render_html
from yuqing.render.ir_migrations import UnsupportedReportVersion, migrate_report
from yuqing.render.validator import ReportValidationError, validate_report

FIXTURE = Path(__file__).parents[3] / "docs" / "方案包" / "fixtures" / "report-ir-v0.1.fixture.json"


def load_fixture():
    if FIXTURE.is_file():
        return json.loads(FIXTURE.read_text(encoding="utf-8"))
    citation = {
        "evidence_ref": "E001",
        "quote_type": "snippet",
        "quote": "该事件已有公开通报。",
        "relation": "support",
        "note": "原文未取得",
    }
    return {
        "schema_version": "0.1",
        "min_reader_minor": 1,
        "report_id": "fixture",
        "task": {"task_id": "t1", "event_query": "离线夹具", "depth": "quick"},
        "metrics": {
            "key_claims_candidate": 1,
            "key_claims_rendered": 1,
            "key_claims_rejected": 0,
        },
        "blocks": [
            {"block_id": "head", "type": "report_header", "section": "00"},
            {
                "block_id": "summary",
                "type": "executive_summary",
                "section": "01",
                "what": [{"text": "该事件已有公开通报。", "claim_ref": "C001"}],
            },
            {
                "block_id": "timeline",
                "type": "timeline",
                "section": "02",
                "nodes": [
                    {
                        "date": "2026-08-12",
                        "text": "公开通报发布。",
                        "claim_ref": "C001",
                        "evidence_refs": ["E001"],
                    }
                ],
            },
            {
                "block_id": "facts",
                "type": "fact_check_table",
                "section": "03",
                "items": [
                    {
                        "claim_ref": "C001",
                        "statement_kind": "fact",
                        "text": "该事件已有公开通报。",
                        "badge": "unverified",
                        "verification_state": "complete",
                        "citations": [citation],
                    }
                ],
            },
            {
                "block_id": "limits",
                "type": "limitations",
                "section": "08",
                "items": [{"id": "L01", "category": "证据强度", "text": "使用搜索摘要。"}],
            },
            {
                "block_id": "appendix",
                "type": "evidence_appendix",
                "section": "09",
                "items": [
                    {
                        "evidence_ref": "E001",
                        "title": "公开通报",
                        "source_name": "监管机构",
                        "source_tier": 1,
                        "url": "https://example.com/notice",
                        "fetch_status": "discovered",
                    }
                ],
            },
        ],
    }


def block(ir, block_type):
    return next(item for item in ir["blocks"] if item["type"] == block_type)


def test_reference_fixture_passes_contract_and_renders_clickable_citations():
    report = load_fixture()
    result = validate_report(report)
    html = render_html(result.report, view="brief")

    assert result.errors == []
    assert "关键事实核查" in html
    evidence_ref = "E002" if FIXTURE.is_file() else "E001"
    assert f'href="#evidence-{evidence_ref}"' in html
    assert f'id="evidence-{evidence_ref}"' in html
    assert "原文未取得" in html
    assert 'class="report-toc"' in html
    assert 'data-section-target="report-section-03"' in html
    assert 'class="console-link"' in html
    assert 'href="/?task=' in html
    assert "--report-width:1120px" in html
    assert "grid-template-columns:minmax(0,1fr) minmax(0,var(--report-width))" in html
    assert "main{grid-column:2;grid-row:1" in html
    assert "font-size:clamp(22px,2.2vw,30px)" in html
    assert "prefers-reduced-motion" in html
    assert "matchMedia('(max-width:900px)')" in html
    assert "link.addEventListener('click',()=>activate(link.dataset.sectionTarget))" in html
    assert "threshold:0" in html


def test_incomplete_verification_is_shown_apart_from_unsupported_evidence():
    report = copy.deepcopy(load_fixture())
    items = block(report, "fact_check_table")["items"]
    # C004 是夹具里唯一本来就判待核验的条目，可安全叠加"核验未完成"（R16 只允许
    # 非 complete 状态配 unverified 徽章）。
    item = next(entry for entry in items if entry["claim_ref"] == "C004")
    item["verification_state"] = "incomplete"
    item["verify_reason"] = "核验未完成（上游不可用）：上游服务返回 503（InternalServerError）"

    html = render_html(validate_report(report).report, view="brief")

    # 同为待核验黄标，但读者必须能看出这条是"没核完"而不是"没依据"。
    assert "核验未完成" in html
    assert "上游服务返回 503" in html


def test_compatible_new_minor_uses_unknown_block_fallback():
    report = load_fixture()
    report["schema_version"] = "0.2"
    report["min_reader_minor"] = 1
    report["blocks"].append(
        {
            "block_id": "b_future",
            "type": "future_block",
            "section": "07",
            "in_brief": True,
            "fallback_text": "新版板块的兼容摘要",
        }
    )

    html = render_html(validate_report(report).report)

    assert "新版板块的兼容摘要" in html


def test_funnel_renders_zero_as_zero_instead_of_blank():
    report = load_fixture()
    report["schema_version"] = "0.4"
    report["min_reader_minor"] = 4
    report["blocks"].append(
        {
            "block_id": "b_funnel_zero",
            "type": "chart",
            "section": "04",
            "in_brief": False,
            "title": "证据获取漏斗",
            "chart_kind": "funnel",
            "data_basis": "evidence_database",
            "items": [{"label": "已取得原文", "value": 0}],
        }
    )

    html = render_html(validate_report(report).report)

    assert "已取得原文</span><strong>0</strong>" in html


@pytest.mark.parametrize(
    ("schema_version", "min_reader_minor"),
    [("0.6", 6), ("1.0", 0)],
)
def test_incompatible_ir_versions_fail_clearly(schema_version, min_reader_minor):
    report = load_fixture()
    report["schema_version"] = schema_version
    report["min_reader_minor"] = min_reader_minor
    with pytest.raises(ReportValidationError, match="R1"):
        validate_report(report)


def test_v01_report_migrates_without_losing_citations_or_history_content():
    report = load_fixture()
    history = {
        "block_id": "history",
        "type": "history_compare",
        "section": "06",
        "title": "历史对照",
        "cards": [
            {
                "event_name": "旧版历史事件",
                "comparison": "旧版对照内容",
                "evidence_refs": ["E001"],
            }
        ],
    }
    report["blocks"].insert(-1, history)

    migrated = migrate_report(report)

    assert migrated["schema_version"] == "0.5"
    assert migrated["min_reader_minor"] == 5
    card = next(
        card
        for block in migrated["blocks"]
        if block["type"] == "history_compare"
        for card in block["cards"]
        if card.get("event_name") == "旧版历史事件"
    )
    assert card["comparison"] == "旧版对照内容"
    assert card["evidence_refs"] == ["E001"]
    assert card["provenance"] == "历史报告迁移"
    assert migrated["migration_history"] == ["0.1->0.2", "0.2->0.3", "0.3->0.4", "0.4->0.5"]


def test_v03_report_migrates_to_reader_that_understands_analytical_blocks():
    report = load_fixture()
    report["schema_version"] = "0.3"
    report["min_reader_minor"] = 3

    migrated = migrate_report(report)

    assert migrated["schema_version"] == "0.5"
    assert migrated["min_reader_minor"] == 5
    assert migrated["migration_history"] == ["0.3->0.4", "0.4->0.5"]


def test_unknown_report_ir_is_rejected_instead_of_silently_rendered():
    with pytest.raises(UnsupportedReportVersion, match="不支持报告 IR 9.0"):
        migrate_report({"schema_version": "9.0", "blocks": []})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ir: ir["blocks"].append(
            {
                "block_id": "bad",
                "type": "chart",
                "section": "04",
                "in_brief": True,
                "data_basis": "illustrative",
            }
        ),
        lambda ir: ir.__setitem__(
            "blocks", [block for block in ir["blocks"] if block["type"] != "limitations"]
        ),
        lambda ir: ir["metrics"].__setitem__("key_claims_candidate", 999),
        lambda ir: block(ir, "fact_check_table")["items"][0].__setitem__("citations", []),
        lambda ir: block(ir, "executive_summary")["what"][0].__setitem__("claim_ref", "C999"),
        lambda ir: block(ir, "timeline")["nodes"][0].__setitem__("evidence_refs", ["E999"]),
        lambda ir: block(ir, "executive_summary")["what"][0].__setitem__(
            "text", "监管部门已认定企业违法"
        ),
        lambda ir: block(ir, "timeline")["nodes"][0].__setitem__("evidence_refs", []),
        lambda ir: block(ir, "fact_check_table")["items"][0]["citations"][0].update(
            {"quote_type": "verbatim", "quote_start": None, "quote_end": 4}
        ),
        # R16：核验未完成时不得带确定性徽章（05 §4.2 / 契约负向样例
        # ir_neg_incomplete_verification）。
        lambda ir: block(ir, "fact_check_table")["items"][0].update(
            {"verification_state": "incomplete", "badge": "verified"}
        ),
    ],
)
def test_negative_contract_cases_are_rejected(mutate):
    report = copy.deepcopy(load_fixture())
    mutate(report)
    with pytest.raises(ReportValidationError):
        validate_report(report)
