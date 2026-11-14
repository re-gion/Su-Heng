import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.executive_summary import (
    COVERAGE_FIELDS,
    check_paragraphs,
    digest,
    review_accepted,
    sentence_ids,
    summary_context,
)
from yuqing.render.html import render_html
from yuqing.render.ir_migrations import migrate_report
from yuqing.render.validator import ReportValidationError, validate_report
from yuqing.services.executive_summary import attach_executive_summary

FIXTURE = Path(__file__).parents[1] / "fixtures" / "executive-summary.json"


@pytest.fixture
def summary_report():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["report"]


def paragraphs():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["paragraphs"]


def approval(value):
    return {
        "sentences": {r: True for r in sentence_ids(value)},
        "coverage": dict.fromkeys(COVERAGE_FIELDS, True),
        "coherent": True,
        "non_redundant": True,
        "no_material_omission": True,
    }


class Gateway:
    def __init__(self, *, rejected=False, error=False):
        self.calls = []
        self.rejected = rejected
        self.error = error

    async def complete_json(self, role, system, prompt, **_kwargs):
        self.calls.append((role, system, prompt))
        if self.error:
            raise TimeoutError("upstream fixture")
        if role == "reporter":
            # A draft's self-reported approval must never be trusted.
            return {"paragraphs": paragraphs(), "narrative_review": approval(paragraphs())}
        result = approval(paragraphs())
        result["sentences"]["S01"] = not self.rejected
        return result


async def attach(report, **options):
    gateway = Gateway(**options)
    await attach_executive_summary(report, OpenAIReportAgent(gateway, "fixture"))
    return gateway, report["blocks"][1]


@pytest.mark.asyncio
async def test_narrative_runs_after_whole_report_and_keeps_authoritative_facts(summary_report):
    before = copy.deepcopy(summary_report["blocks"][2])
    gateway, block = await attach(summary_report)
    assert [call[0] for call in gateway.calls] == ["reporter", "verifier"]
    assert "analysis:0" in gateway.calls[0][2] and "L01" in gateway.calls[0][2]
    assert block["summary_mode"] == "narrative"
    assert summary_report["blocks"][2]["items"] == before["items"]
    assert summary_report["quality"]["release_gate_missing"] == ["media_propagation"]
    assert summary_report["quality"]["release_label"] == "evidence_brief"
    assert validate_report(summary_report).errors == []
    for view in ("brief", "full"):
        html = render_html(summary_report, view=view)
        assert 'class="summary-narrative"' in html
        assert 'href="#claim-C001"' in html and 'href="#analysis"' in html
        assert 'href="#limitation-L01"' in html
        assert "关键进展及其核验状态" not in html
        assert "优先决策事项（分析判断）" not in html
        assert "本报告围绕" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("options,expected_calls", [({"rejected": True}, 4), ({"error": True}, 1)])
async def test_rejected_or_unavailable_summary_falls_back_without_leaking_draft(
    summary_report, options, expected_calls
):
    gateway, block = await attach(summary_report, **options)
    assert len(gateway.calls) == expected_calls
    assert block["summary_mode"] == "fact_brief"
    assert "paragraphs" not in block and "narrative_review" not in block
    assert "executive_summary" in summary_report["quality"]["release_gate_missing"]
    html = render_html(summary_report)
    assert "以下为事实提要" in html
    assert "据此，本报告认为" not in html
    assert validate_report(summary_report).errors == []


@pytest.mark.parametrize(
    "corruption", ["missing", "string_true", "extra_id", "omission", "coverage", "coherent"]
)
def test_partial_or_ambiguous_review_never_passes(corruption):
    value = paragraphs()
    review = approval(value)
    if corruption == "missing":
        review["sentences"].pop("S02")
    elif corruption == "string_true":
        review["sentences"]["S01"] = "true"
    elif corruption == "extra_id":
        review["sentences"]["S99"] = True
    elif corruption == "omission":
        review["no_material_omission"] = False
    elif corruption == "coverage":
        review["coverage"].pop("current_state")
    else:
        review["coherent"] = False
    assert not review_accepted(review, value)


@pytest.mark.parametrize(
    "change,error",
    [
        ("number", "unsupported_number"),
        ("unit", "unsupported_number"),
        ("ref", "summary_reference"),
        ("duplicate", "duplicate_sentence"),
        ("authority", "sentence_shape"),
        ("kind", "summary_kind"),
        ("internal", "internal_process_leak"),
    ],
)
def test_deterministic_guards_reject_invalid_bindings_and_new_numbers(
    summary_report, change, error
):
    value = paragraphs()
    first = value[0]["sentences"][0]
    if change == "number":
        first["text"] = "已解决999条诉求。"
    elif change == "unit":
        first["text"] = "受理占比120%。"
    elif change == "ref":
        first["claim_refs"] = ["C999"]
    elif change == "duplicate":
        value.append(copy.deepcopy(value[0]))
    elif change == "authority":
        first["badge"] = "verified"
    elif change == "kind":
        first["kind"] = "analysis"
    else:
        first["text"] = "从已审分析看，诉求进入核查。"
    assert error in check_paragraphs(value, summary_context(summary_report))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["sentence", "fact", "analysis", "limitation", "approval"])
async def test_replay_rejects_changed_text_or_basis(summary_report, change):
    _, block = await attach(summary_report)
    if change == "sentence":
        block["paragraphs"][0]["sentences"][0]["text"] = "校方已解决全部诉求。"
    elif change == "fact":
        summary_report["blocks"][2]["items"][0]["badge"] = "verified"
    elif change == "analysis":
        summary_report["blocks"][3]["items"][0]["interpretation"] = "全部诉求已解决。"
    elif change == "limitation":
        summary_report["blocks"][4]["items"][0]["text"] = "材料没有局限。"
    else:
        block["narrative_review"] = None
    with pytest.raises(ReportValidationError, match="R25"):
        validate_report(summary_report)
    with pytest.raises(ValueError, match="摘要"):
        render_html(summary_report)


@pytest.mark.asyncio
async def test_scope_redaction_invalidates_semantic_approval(summary_report):
    class ScopeReviewer:
        async def review(self, texts, **_kwargs):
            return [
                SimpleNamespace(allowed=True, text=t.replace("校方", "有关部门")) for t in texts
            ]

    await attach_executive_summary(
        summary_report, OpenAIReportAgent(Gateway(), "fixture"), scope_reviewer=ScopeReviewer()
    )
    assert summary_report["blocks"][1]["summary_mode"] == "fact_brief"
    assert summary_report["blocks"][1]["summary_status"] == "scope_review_incomplete"


@pytest.mark.asyncio
async def test_scope_reviewer_required_cannot_be_skipped(summary_report):
    gateway = Gateway()
    await attach_executive_summary(
        summary_report, OpenAIReportAgent(gateway, "fixture"), scope_required=True
    )
    assert gateway.calls == []
    assert summary_report["blocks"][1]["summary_mode"] == "fact_brief"


@pytest.mark.asyncio
async def test_only_approved_summary_is_cached_and_changed_basis_invalidates_it(summary_report):
    saved = {}

    class Database:
        async def get_analysis_batch(self, task, stage, fingerprint):
            return copy.deepcopy(saved.get(fingerprint))

        async def save_analysis_batch(self, task, stage, fingerprint, value):
            saved[fingerprint] = copy.deepcopy(value)

    gateway = Gateway()
    agent = OpenAIReportAgent(gateway, "fixture")
    agent.bind(Database(), "summary-test")
    context = summary_context(summary_report)
    await agent.summarize(context)
    await agent.summarize(context)
    assert len(gateway.calls) == 2
    context["limitations"]["L02"] = {"id": "L02", "text": "尚缺独立来源。"}
    await agent.summarize(context)
    assert len(gateway.calls) == 4
    gateway.rejected = True
    context["task"]["event_query"] = "不同事件"
    assert (await agent.summarize(context))["status"] == "rejected"
    assert len(saved) == 2


def test_old_report_migration_preserves_summary_without_synthesizing_new_prose(summary_report):
    summary_report.update(schema_version="0.9", min_reader_minor=9)
    block = summary_report["blocks"][1]
    block.pop("summary_mode")
    before = copy.deepcopy(block)
    migrated = migrate_report(summary_report)
    assert migrated["blocks"][1] == before
    assert migrated["migration_history"] == ["0.9->0.10"]
    assert "关键进展及其核验状态" in render_html(migrated)


@pytest.mark.asyncio
async def test_model_self_approval_cannot_replace_verifier(summary_report):
    class SelfApprovingGateway(Gateway):
        async def complete_json(self, role, *args, **kwargs):
            if role == "verifier":
                return {"accepted": True}
            return await super().complete_json(role, *args, **kwargs)

    await attach_executive_summary(
        summary_report, OpenAIReportAgent(SelfApprovingGateway(), "fixture")
    )
    assert summary_report["blocks"][1]["summary_mode"] == "fact_brief"


def test_unreviewed_analysis_is_not_summary_material(summary_report):
    summary_report["quality"]["semantic_review"] = {"status": "not_provided"}
    assert summary_context(summary_report)["analyses"] == {}


@pytest.mark.asyncio
async def test_digest_excludes_runtime_telemetry(summary_report):
    await attach(summary_report)
    before = digest(summary_context(summary_report))
    summary_report["quality"]["timing"] = {"active_seconds": 123}
    summary_report["task"]["generated_at"] = "2026-11-14T12:00:00+08:00"
    assert digest(summary_context(summary_report)) == before
    assert validate_report(summary_report).errors == []


@pytest.mark.asyncio
async def test_same_model_review_retains_independence_notice(summary_report):
    gateway = Gateway()
    gateway.factory = SimpleNamespace(config=lambda _role: SimpleNamespace(model="same-model"))
    await attach_executive_summary(summary_report, OpenAIReportAgent(gateway, "fixture"))
    assert summary_report["blocks"][1]["summary_mode"] == "narrative"
    assert any("同一模型" in i["text"] for i in summary_report["blocks"][4]["items"])
    assert validate_report(summary_report).errors == []


def test_only_reviewed_comment_observations_enter_summary_as_sample_material(summary_report):
    summary_report["blocks"].append(
        {
            "block_id": "comments",
            "type": "comment_insight",
            "analysis_version": 5,
            "observations": [
                {
                    "id": "O1",
                    "text": "部分样本询问处理进展。",
                    "review_status": "accepted",
                    "evidence_refs": ["E001"],
                },
                {"id": "O2", "text": "未审观点不可使用。", "review_status": "pending"},
            ],
            "items": [
                {
                    "id": "Q1",
                    "title": "处理进展",
                    "review_status": "accepted",
                    "observation_refs": ["O1"],
                    "sample_count": 1,
                },
                {
                    "id": "Q2",
                    "title": "未审问题",
                    "review_status": "accepted",
                    "observation_refs": ["O2"],
                },
            ],
        }
    )
    context = summary_context(summary_report)
    assert "comments:Q1" in context["analyses"]
    assert "comments:Q2" not in context["analyses"]
    record = context["analyses"]["comments:Q1"]
    assert record["kind"] == "comment_sample" and "不代表总体民意" in record["uncertainty"]


def test_analysis_can_cite_its_explicit_limitation_together(summary_report):
    value = paragraphs()
    value[1]["sentences"][0]["limitation_refs"] = ["L01"]
    assert check_paragraphs(value, summary_context(summary_report)) == []
