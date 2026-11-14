import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.executive_summary import (
    COVERAGE_FIELDS,
    PARAGRAPH_REVIEW_FIELDS,
    SUMMARY_HEADINGS,
    SUMMARY_ROLES,
    check_outline,
    check_paragraphs,
    digest,
    review_accepted,
    sentence_ids,
    summary_context,
)
from yuqing.render.html import render_html
from yuqing.render.ir_migrations import migrate_report
from yuqing.render.validator import ReportValidationError, validate_report
from yuqing.services.executive_summary import attach_executive_summary, safe_summary_diagnostics

FIXTURE = Path(__file__).parents[1] / "fixtures" / "executive-summary.json"


@pytest.fixture
def summary_report():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["report"]


def paragraphs():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["paragraphs"]


def approval(value):
    result = {
        "sentences": {r: True for r in sentence_ids(value)},
        "coverage": dict.fromkeys(COVERAGE_FIELDS, True),
        "coherent": True,
        "non_redundant": True,
        "no_material_omission": True,
    }
    if all("role" in p for p in value):
        result["paragraphs"] = {
            role: dict.fromkeys(PARAGRAPH_REVIEW_FIELDS, True) for role in SUMMARY_ROLES
        }
        result["no_background_rewind"] = True
    return result


class Gateway:
    def __init__(self, *, rejected=False, error=False):
        self.calls = []
        self.rejected = rejected
        self.error = error

    async def complete_json(self, role, system, prompt, **_kwargs):
        self.calls.append((role, system, prompt))
        if self.error:
            raise TimeoutError("upstream fixture")
        if role == "reporter" and "阶段：四段摘要规划。" in prompt:
            return {"outline": json.loads(FIXTURE.read_text(encoding="utf-8"))["outline"]}
        if role == "reporter":
            # A draft's self-reported approval must never be trusted.
            return {"paragraphs": paragraphs(), "narrative_review": approval(paragraphs())}
        result = approval(paragraphs())
        result["sentences"]["S01"] = not self.rejected
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("omission", [False, True])
async def test_remove_only_located_unsupported_sentence_then_require_fresh_full_review(
    summary_report, omission
):
    class NoisyGateway(Gateway):
        checks = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            value = await super().complete_json(role, system, prompt, **kwargs)
            if role == "reporter" and "paragraphs" in value:
                value["paragraphs"][2]["sentences"].append(
                    {
                        "text": "材料已产生999元的新增费用。",
                        "kind": "fact",
                        "claim_refs": ["C001"],
                        "analysis_refs": [],
                        "limitation_refs": [],
                    }
                )
            if role == "verifier":
                self.checks += 1
                payload = json.loads(prompt.split("\n", 1)[1])
                assert all(
                    "999" not in s["text"] for p in payload["paragraphs"] for s in p["sentences"]
                )
                value["no_material_omission"] = not omission
            return value

    gateway = NoisyGateway()
    await attach_executive_summary(summary_report, OpenAIReportAgent(gateway, "fixture"))
    summary = next(b for b in summary_report["blocks"] if b["type"] == "executive_summary")
    assert gateway.checks == 1
    assert summary["summary_mode"] == ("fact_brief" if omission else "narrative")
    if not omission:
        assert summary["paragraphs"] == paragraphs()
        assert summary["narrative_review"]["repair_used"] is True


@pytest.mark.asyncio
async def test_type_reference_repair_explains_the_failed_sentence_contract(summary_report):
    class TypeMismatchGateway(Gateway):
        drafts = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            value = await super().complete_json(role, system, prompt, **kwargs)
            if role == "reporter" and "paragraphs" in value:
                self.drafts += 1
                if self.drafts == 1:
                    sentence = value["paragraphs"][-1]["sentences"][-1]
                    sentence.update(kind="analysis", analysis_refs=[], limitation_refs=["L01"])
                else:
                    feedback = json.loads(prompt[prompt.index('{"report":') :])["repair_feedback"]
                    assert feedback["issues"][0]["path"].startswith("paragraphs[3]")
                    explanation = feedback.get("issue_explanations", {}).get("summary_kind", "")
                    assert "analysis_refs" in explanation and "limitation_refs" in explanation
                    assert "不能" in explanation
            return value

    gateway = TypeMismatchGateway()
    result = await OpenAIReportAgent(gateway, "fixture").summarize(summary_context(summary_report))
    assert gateway.drafts == 2
    assert result.get("status") not in {"unavailable", "rejected"}


async def attach(report, **options):
    gateway = Gateway(**options)
    await attach_executive_summary(report, OpenAIReportAgent(gateway, "fixture"))
    return gateway, report["blocks"][1]


@pytest.mark.asyncio
async def test_narrative_runs_after_whole_report_and_keeps_authoritative_facts(summary_report):
    before = copy.deepcopy(summary_report["blocks"][2])
    gateway, block = await attach(summary_report)
    assert [call[0] for call in gateway.calls] == ["reporter", "reporter", "verifier"]
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
@pytest.mark.parametrize("options,expected_calls", [({"rejected": True}, 5), ({"error": True}, 1)])
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
        value[0]["sentences"].append(copy.deepcopy(value[0]["sentences"][0]))
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
    assert len(gateway.calls) == 3
    context["limitations"]["L02"] = {"id": "L02", "text": "尚缺独立来源。"}
    await agent.summarize(context)
    assert len(gateway.calls) == 6
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
    assert migrated["migration_history"] == ["0.9->0.10", "0.10->0.11", "0.11->0.12"]
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


def test_compact_context_preserves_facts_and_relations_and_includes_chart_timeline(summary_report):
    summary_report["blocks"][2]["items"][0]["verify_reason"] = "冗余审查过程记录。" * 500
    summary_report["blocks"].append(
        {
            "type": "chart",
            "chart_kind": "timeline",
            "items": [{"date": "2026-11-14", "text": "公开受理通报。", "claim_refs": ["C001"]}],
        }
    )
    full = summary_context(summary_report, version="narrative-v1")
    compact = summary_context(summary_report)
    assert full["timeline"] == [] and len(compact["timeline"]) == 1
    assert len(json.dumps(compact)) < len(json.dumps(full))
    for ref, fact in full["facts"].items():
        assert compact["facts"][ref]["text"] == fact["text"]
        assert compact["facts"][ref]["badge"] == fact["badge"]
        citations = compact["facts"][ref]["citations"]
        assert len(citations) == len(fact["citations"])
        for old, new in zip(fact["citations"], citations, strict=True):
            for field in ("evidence_ref", "quote", "relation", "fetch_status", "source_role"):
                assert old[field] == new[field]
    before = digest(compact)
    summary_report["blocks"][2]["items"][0]["verify_reason"] = "材料关系变化。"
    assert digest(summary_context(summary_report)) != before


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
    value[2]["sentences"][0]["limitation_refs"] = ["L01"]
    assert check_paragraphs(value, summary_context(summary_report)) == []


@pytest.mark.asyncio
async def test_factual_summary_with_source_boundary_is_not_downgraded(summary_report):
    class BoundaryGateway(Gateway):
        async def complete_json(self, role, *args, **kwargs):
            if role == "reporter" and "阶段：四段摘要规划。" not in args[1]:
                value = paragraphs()
                value[0]["sentences"][0]["limitation_refs"] = ["L01"]
                return {"paragraphs": value}
            return await super().complete_json(role, *args, **kwargs)

    await attach_executive_summary(summary_report, OpenAIReportAgent(BoundaryGateway(), "fixture"))
    assert summary_report["blocks"][1]["summary_mode"] == "narrative"
    assert "executive_summary" not in summary_report["quality"]["release_gate_missing"]


@pytest.mark.asyncio
async def test_structure_repair_receives_previous_draft_and_actionable_limits(summary_report):
    class RepairGateway(Gateway):
        attempts = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "reporter" and "阶段：四段摘要规划。" not in prompt:
                self.attempts += 1
                value = paragraphs()
                if self.attempts == 1:
                    # Reproduces the real-task output exceeding the hard length bound.
                    sentence = value[0]["sentences"][0]
                    value[0]["sentences"] = [
                        {
                            **sentence,
                            "text": f"校方通报称，{i}。" + "相关事项仍需依据公开材料理解。" * 10,
                        }
                        for i in range(10)
                    ]
                    return {"paragraphs": value}
                feedback = json.loads(prompt.split("\n", 1)[1])["repair_feedback"]
                assert feedback["previous_paragraphs"]
                assert feedback["limits"]["max_characters"] == 1400
                assert feedback["issues"]
                return {"paragraphs": value}
            return await super().complete_json(role, system, prompt, **kwargs)

    gateway = RepairGateway()
    await attach_executive_summary(summary_report, OpenAIReportAgent(gateway, "fixture"))
    assert gateway.attempts == 2
    assert summary_report["blocks"][1]["summary_mode"] == "narrative"
    diagnostic = summary_report["quality"]["executive_summary"]["diagnostics"][0]
    assert diagnostic["stage"] == "structure" and "summary_too_long" in diagnostic["codes"]


def test_factual_boundary_reference_does_not_replace_fact_or_allow_analysis(summary_report):
    value = paragraphs()
    sentence = value[0]["sentences"][0]
    sentence["limitation_refs"] = ["L01"]
    sentence["claim_refs"] = []
    assert "summary_kind" in check_paragraphs(value, summary_context(summary_report))
    sentence["claim_refs"] = ["C001"]
    sentence["analysis_refs"] = ["analysis:0"]
    assert "summary_kind" in check_paragraphs(value, summary_context(summary_report))


@pytest.mark.asyncio
async def test_downgrade_reports_controlled_reasons_without_model_feedback(summary_report):
    class RejectGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if role == "verifier":
                result["sentences"]["S01"] = False
                result["feedback"] = "PRIVATE_UNREVIEWED_REJECTION_TEXT"
            return result

    await attach_executive_summary(summary_report, OpenAIReportAgent(RejectGateway(), "fixture"))
    diagnostics = summary_report["quality"]["executive_summary"]["diagnostics"]
    assert diagnostics and "content_review_rejected" in diagnostics[0]["codes"]
    assert {"path": "sentences.S01", "codes": ["sentence_not_supported"]} in diagnostics[0][
        "issues"
    ]
    assert "PRIVATE_UNREVIEWED_REJECTION_TEXT" not in json.dumps(summary_report)
    html = render_html(summary_report)
    assert "摘要未完成原因" in html and "摘要内容或整体覆盖未通过审查" in html
    assert "PRIVATE_UNREVIEWED_REJECTION_TEXT" not in html


@pytest.mark.asyncio
async def test_paragraph_headings_are_reviewed_and_displayed(summary_report):
    headings = list(SUMMARY_HEADINGS.values())

    class HeadingGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "reporter" and "阶段：四段摘要规划。" not in prompt:
                value = paragraphs()
                for paragraph, heading in zip(value, headings, strict=True):
                    paragraph["heading"] = heading
                return {"paragraphs": value}
            if role == "verifier":
                for heading in headings:
                    assert heading in prompt
            return await super().complete_json(role, system, prompt, **kwargs)

    await attach_executive_summary(summary_report, OpenAIReportAgent(HeadingGateway(), "fixture"))
    assert summary_report["blocks"][1]["summary_mode"] == "narrative"
    html = render_html(summary_report)
    for heading in headings:
        assert f'<h3 class="summary-heading">{heading}</h3>' in html
    assert validate_report(summary_report).errors == []


@pytest.mark.asyncio
async def test_semantic_repair_locates_unsupported_sentence_and_missing_dispute(summary_report):
    class RepairGateway(Gateway):
        reviews = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "reporter" and self.reviews and "阶段：四段摘要规划。" not in prompt:
                feedback = json.loads(prompt.split("\n", 1)[1])["repair_feedback"]
                assert {"path": "sentences.S01", "codes": ["sentence_not_supported"]} in feedback[
                    "issues"
                ]
                assert {"path": "coverage.dispute", "codes": ["coverage_gap"]} in feedback["issues"]
                assert feedback["previous_paragraphs"] and "本次是修订" in prompt
            result = await super().complete_json(role, system, prompt, **kwargs)
            if role == "verifier":
                self.reviews += 1
                if self.reviews == 1:
                    result["sentences"]["S01"] = False
                    result["coverage"]["dispute"] = False
                    result["feedback"] = "PRIVATE_REVIEW_FEEDBACK"
            return result

    await attach_executive_summary(summary_report, OpenAIReportAgent(RepairGateway(), "fixture"))
    assert summary_report["blocks"][1]["summary_mode"] == "narrative"
    assert "PRIVATE_REVIEW_FEEDBACK" not in json.dumps(summary_report)
    assert summary_report["blocks"][1]["narrative_review"]["repair_used"] is True


@pytest.mark.asyncio
async def test_heading_changes_invalidate_approval(summary_report):
    await attach(summary_report)
    summary_report["blocks"][1]["paragraphs"][0]["heading"] = "全部诉求已经解决"
    with pytest.raises(ReportValidationError, match="R25"):
        validate_report(summary_report)


@pytest.mark.parametrize(
    "heading,error",
    [
        ("", "heading_text"),
        ("<b>结果</b>", "heading_text"),
        ("长标题" * 12, "heading_text"),
        ("已解决999条诉求", "unsupported_number"),
    ],
)
def test_invalid_or_unsupported_headings_are_rejected(summary_report, heading, error):
    value = paragraphs()
    value[0]["heading"] = heading
    assert error in check_paragraphs(value, summary_context(summary_report), require_headings=True)


@pytest.mark.asyncio
async def test_old_narrative_migration_does_not_invent_headings(summary_report):
    await attach(summary_report)
    block = summary_report["blocks"][1]
    for p in block["paragraphs"]:
        p.pop("heading")
        p.pop("role", None)
        p.pop("availability", None)
    # A historical fixture with the original, headerless content approval.
    block["narrative_review"]["content_digest"] = digest(block["paragraphs"])
    summary_report.update(schema_version="0.10", min_reader_minor=10)
    block["narrative_review"]["version"] = "narrative-v1"
    block["narrative_review"]["basis_digest"] = digest(
        summary_context(summary_report, version="narrative-v1")
    )
    before = copy.deepcopy(block)
    assert validate_report(summary_report).errors == []
    migrated = migrate_report(summary_report)
    assert migrated["blocks"][1] == before
    assert migrated["migration_history"] == ["0.10->0.11", "0.11->0.12"]
    assert '<h3 class="summary-heading">' not in render_html(migrated)


def test_diagnostic_filter_does_not_publish_model_prose_or_arbitrary_paths():
    result = safe_summary_diagnostics(
        [
            {
                "stage": "semantic",
                "attempt": 1,
                "codes": ["sentence_not_supported", "PRIVATE_TEXT"],
                "message": "PRIVATE_TEXT",
                "issues": [
                    {
                        "path": "sentences.S01",
                        "codes": ["sentence_not_supported"],
                        "text": "PRIVATE_TEXT",
                    },
                    {"path": "PRIVATE_PATH", "codes": ["sentence_not_supported"]},
                ],
            },
            {"stage": {}, "codes": ["invalid_review"]},
        ]
    )
    assert result == [
        {
            "stage": "semantic",
            "attempt": 1,
            "codes": ["sentence_not_supported"],
            "issues": [{"path": "sentences.S01", "codes": ["sentence_not_supported"]}],
        }
    ]


@pytest.mark.parametrize(
    "role,field",
    [
        ("event", "role_match"),
        ("dispute", "focused"),
        ("findings", "role_match"),
        ("limits", "gap_disclosed"),
    ],
)
def test_global_coverage_does_not_replace_paragraph_role_review(role, field):
    value = paragraphs()
    review = approval(value)
    assert all(review["coverage"].values()) and review["coherent"] is True
    review["paragraphs"][role][field] = False
    assert not review_accepted(review, value)


@pytest.mark.parametrize(
    "corruption",
    ["missing_role", "extra_role", "missing_field", "string_true", "background_rewind"],
)
def test_paragraph_review_must_be_complete_and_strict(corruption):
    value = paragraphs()
    review = approval(value)
    if corruption == "missing_role":
        review["paragraphs"].pop("dispute")
    elif corruption == "extra_role":
        review["paragraphs"]["other"] = dict.fromkeys(PARAGRAPH_REVIEW_FIELDS, True)
    elif corruption == "missing_field":
        review["paragraphs"]["limits"].pop("focused")
    elif corruption == "string_true":
        review["paragraphs"]["event"]["role_match"] = "true"
    else:
        review["no_background_rewind"] = False
    assert not review_accepted(review, value)


@pytest.mark.parametrize(
    "corruption", ["reordered", "duplicate", "three_paragraphs", "missing_role"]
)
def test_four_roles_and_their_order_are_a_contract(summary_report, corruption):
    value = paragraphs()
    if corruption == "reordered":
        value[1], value[2] = value[2], value[1]
    elif corruption == "duplicate":
        value[1]["role"] = "event"
    elif corruption == "three_paragraphs":
        value.pop(1)
    else:
        value[1].pop("role")
    assert "paragraph_roles" in check_paragraphs(value, summary_context(summary_report))


@pytest.mark.asyncio
async def test_wandering_paragraphs_are_rejected_even_when_global_review_passes(summary_report):
    class WanderingGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if role == "verifier":
                result["paragraphs"]["dispute"]["role_match"] = False
                result["paragraphs"]["limits"]["focused"] = False
                result["no_background_rewind"] = False
            return result

    gateway = WanderingGateway()
    await attach_executive_summary(summary_report, OpenAIReportAgent(gateway, "fixture"))
    assert summary_report["blocks"][1]["summary_mode"] == "fact_brief"
    codes = summary_report["blocks"][1]["summary_diagnostics"][0]["codes"]
    assert {"paragraph_role_mismatch", "paragraph_unfocused", "summary_background_rewind"} <= set(
        codes
    )
    assert "executive_summary" in summary_report["quality"]["release_gate_missing"]


@pytest.mark.asyncio
async def test_insufficient_event_stops_before_drafting(summary_report):
    class MissingEventGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "outline" in result:
                result["outline"][0]["availability"] = "insufficient"
                result["outline"][0]["limitation_refs"] = ["L01"]
            return result

    gateway = MissingEventGateway()
    await attach_executive_summary(summary_report, OpenAIReportAgent(gateway, "fixture"))
    assert len(gateway.calls) == 1
    assert summary_report["blocks"][1]["summary_mode"] == "fact_brief"
    assert (
        "event_outline_insufficient"
        in summary_report["blocks"][1]["summary_diagnostics"][0]["codes"]
    )


def test_insufficient_paragraph_keeps_its_role_and_must_disclose_gap(summary_report):
    value = paragraphs()
    assert len(value) == 4 and value[1]["availability"] == "insufficient"
    assert check_paragraphs(value, summary_context(summary_report)) == []
    value[1]["sentences"] = [copy.deepcopy(value[0]["sentences"][0])]
    assert "paragraph_availability" in check_paragraphs(value, summary_context(summary_report))


@pytest.mark.asyncio
async def test_fixed_headings_and_planning_notes_are_not_draft_authority(summary_report):
    class UntrustedHeadingGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "outline" in result:
                result["outline"][1]["focus"] = "PRIVATE_TRANSIENT_OUTLINE_FOCUS"
            if "paragraphs" in result and role == "reporter":
                for paragraph in result["paragraphs"]:
                    paragraph["heading"] = "不受支持的结论标题"
                    paragraph["availability"] = "supported"
            return result

    await attach_executive_summary(
        summary_report, OpenAIReportAgent(UntrustedHeadingGateway(), "fixture")
    )
    stored = summary_report["blocks"][1]
    assert stored["summary_mode"] == "narrative"
    assert [p["heading"] for p in stored["paragraphs"]] == list(SUMMARY_HEADINGS.values())
    assert stored["paragraphs"][1]["availability"] == "insufficient"
    assert "PRIVATE_TRANSIENT_OUTLINE_FOCUS" not in json.dumps(summary_report)


def test_outline_rejects_wrong_evidence_and_omitted_roles(summary_report):
    context = summary_context(summary_report)
    outline = json.loads(FIXTURE.read_text(encoding="utf-8"))["outline"]
    assert check_outline(outline, context) == []
    outline[1]["limitation_refs"] = ["L999"]
    assert "summary_outline_invalid" in check_outline(outline, context)
    assert "summary_outline_invalid" in check_outline(outline[:3], context)


def test_method_notice_stays_in_audit_chapter_and_legacy_context(summary_report):
    notice = {"id": "LS01", "category": "摘要审查独立性", "text": "写作与审查使用同一模型。"}
    summary_report["blocks"][4]["items"].append(notice)
    context = summary_context(summary_report)
    assert "LS01" not in context["limitations"]
    assert "LS01" in summary_context(summary_report, version="narrative-v2")["limitations"]
    assert notice in summary_report["blocks"][4]["items"]
    summary_report["blocks"][4]["items"][-1]["text"] = "方法说明已变更。"
    assert digest(summary_context(summary_report)) != digest(context)


@pytest.mark.asyncio
async def test_legacy_v2_prose_is_not_relabelled_during_migration(summary_report):
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))["legacy_paragraphs"]
    block = summary_report["blocks"][1]
    context = summary_context(summary_report, version="narrative-v2")
    block.update(
        summary_mode="narrative",
        paragraphs=value,
        narrative_review={
            **approval(value),
            "version": "narrative-v2",
            "content_digest": digest(value),
            "basis_digest": digest(context),
        },
    )
    summary_report.update(schema_version="0.11", min_reader_minor=11)
    before = copy.deepcopy(block)
    migrated = migrate_report(summary_report)
    assert migrated["blocks"][1] == before
    assert validate_report(migrated).errors == []
    assert "核心判断" in render_html(migrated)
