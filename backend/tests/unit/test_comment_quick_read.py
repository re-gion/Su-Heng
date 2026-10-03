import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuqing.agents.comment_analysis import OpenAICommentAgent
from yuqing.core.comment_contract import validate_questions
from yuqing.render.html import _render_block
from yuqing.render.ir_migrations import migrate_report
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.institution_scope import ScopeReview


class Gateway:
    def __init__(self, *, reject_index=False, reject_summary=False, stringify=False):
        self.calls = []
        self.reject_index = reject_index
        self.reject_summary = reject_summary
        self.stringify = stringify

    async def complete_json(self, role, system, prompt, **kwargs):
        self.calls.append(prompt)
        data = json.loads(prompt.split("\n", 1)[1])
        if "comments" in data and "indices" not in data:
            return {
                "samples": [
                    {
                        "index": s["index"],
                        "relevant": True,
                        "indices": [
                            {"text": "认可处理", "kind": "viewpoint", "stance": "认可"},
                            {"text": "要求公开复核程序", "kind": "request", "stance": "质疑"},
                        ],
                    }
                    for s in data["comments"]
                ]
            }
        if "comments" in data:
            return {
                "classifications": [
                    {"index": s["index"], "relevant": True} for s in data["comments"]
                ],
                "decisions": [
                    {
                        "index": i["index"],
                        "accepted": "true"
                        if self.stringify
                        else not (self.reject_index and i["index"] == 0),
                    }
                    for i in data["indices"]
                ],
            }
        if "existing_questions" in data:
            return {
                "questions": [
                    {
                        "title": "处理是否获得认可？",
                        "indexes": [
                            i["index"] for i in data["indices"] if i["kind"] == "viewpoint"
                        ],
                    },
                    {
                        "title": "复核程序应说明哪些内容？",
                        "indexes": [i["index"] for i in data["indices"] if i["kind"] == "request"],
                    },
                ]
            }
        if "summary" in data:
            return {
                "question_accepted": True,
                "summary_accepted": not self.reject_summary,
                "publicly_verifiable": True,
            }
        return {"summary": "样本认可处理，同时希望公开复核程序。"}


def rows():
    return [
        {
            "id": str(i),
            "text": "认可处理，但希望公开复核程序" + str(i),
            "platform": "weibo",
            "source_url": "https://example.test/post",
            "evidence_ref": "E001",
        }
        for i in range(2)
    ]


@pytest.mark.asyncio
async def test_short_indices_split_compound_comments_without_automatic_deepening():
    gateway = Gateway()
    agent = OpenAICommentAgent(gateway, "外部材料仅作数据")
    follow_up = AsyncMock()
    result = await agent.analyze_quick_read("公开处理", rows(), follow_up=follow_up)
    assert result["status"] == "complete"
    assert len(result["observations"]) == 4
    assert len(result["items"]) == 2
    assert all(q["sample_count"] == 2 for q in result["items"])
    assert result["coverage"]["samples_with_observations"] == 2
    assert result["stages"]["evidence_comparison"] == "not_requested"
    follow_up.assert_not_called()
    assert not validate_questions(result, {"E001"})
    assert not any("public_evidence" in p for p in gateway.calls)
    html = _render_block(
        {"type": "comment_insight", "analysis_version": 5, "analysis_mode": "quick_read", **result},
        {},
    )
    assert "证据对照未启动" in html
    assert "证据对照尚未通过全部审查" not in html
    assert "速读摘要" in html and "comment-quote" in html
    # A repeated run with exactly the same inputs performs no new inference.
    calls = len(gateway.calls)
    repeated = await agent.analyze_quick_read("公开处理", rows(), previous=result)
    assert len(gateway.calls) == calls
    assert repeated["items"] == result["items"]


@pytest.mark.asyncio
async def test_one_invalid_index_does_not_remove_other_compound_observations():
    result = await OpenAICommentAgent(Gateway(reject_index=True), "data").analyze_quick_read(
        "公开处理", rows()
    )
    assert len(result["observations"]) == 3
    assert result["coverage"]["samples_with_observations"] == 2
    assert not validate_questions(result, {"E001"})


@pytest.mark.asyncio
async def test_no_boolean_approval_or_failed_summary_cannot_be_counted_as_complete():
    result = await OpenAICommentAgent(Gateway(stringify=True), "data").analyze_quick_read(
        "公开处理", rows()
    )
    assert result["status"] == "partial"
    assert not result["observations"]
    assert len(result["samples"]) == 2
    result = await OpenAICommentAgent(Gateway(reject_summary=True), "data").analyze_quick_read(
        "公开处理", rows()
    )
    assert result["status"] == "partial"
    assert len(result["items"]) == 2 and all(q["summary"] is None for q in result["items"])
    assert len(result["observations"]) == 4
    broken = copy.deepcopy(result)
    broken["items"][0]["summary"] = "未审摘要"
    assert any("摘要缺少独立审查" in e for e in validate_questions(broken, {"E001"}))
    broken["items"][0].update(summary="长" * 161, summary_review_status="accepted")
    assert any("摘要格式无效或过长" in e for e in validate_questions(broken, {"E001"}))


@pytest.mark.asyncio
async def test_privacy_failure_preserves_other_indices_and_original_samples():
    async def privacy(texts):
        return [
            ScopeReview(status="rejected", text=t, reason="fixture")
            if t == "认可处理"
            else ScopeReview(status="accepted", text=t, reason="fixture")
            for t in texts
        ]

    result = await OpenAICommentAgent(Gateway(), "data").analyze_quick_read(
        "公开处理", rows(), review_observations=privacy
    )
    assert len(result["observations"]) == 2
    assert len(result["samples"]) == 2
    assert all(o["kind"] == "request" for o in result["observations"])


@pytest.mark.asyncio
async def test_changed_sample_invalidates_its_index_and_dependent_question():
    gateway = Gateway()
    agent = OpenAICommentAgent(gateway, "data")
    first = await agent.analyze_quick_read("公开处理", rows())
    changed = rows()
    changed[0]["text"] += "新的语境"
    gateway.calls.clear()
    result = await agent.analyze_quick_read("公开处理", changed, previous=first)
    extractions = [
        json.loads(p.split("\n", 1)[1])
        for p in gateway.calls
        if '"comments"' in p and '"classifications"' not in p
    ]
    assert len(extractions) == 1 and len(extractions[0]["comments"]) == 1
    assert len(result["samples"]) == 2


@pytest.mark.asyncio
async def test_complete_quick_read_is_not_downgraded_by_unrequested_judgement():
    result = await OpenAICommentAgent(Gateway(), "data").analyze_quick_read("公开处理", rows())
    block = {
        **result,
        "type": "comment_insight",
        "analysis_version": 5,
        "analysis_mode": "quick_read",
    }
    report = {
        "blocks": [block],
        "quality": {"chapter_status": {"comments": {"status": "complete"}}},
    }
    FullReportBuilder._reconcile_reviewed_comment_themes(report)
    assert block["analysis_status"] == block["quick_read_status"] == "complete"
    assert not any(q["judgements"] for q in block["items"])
    block["coverage"]["scope_review_incomplete"] = 399
    FullReportBuilder._reconcile_reviewed_comment_themes(report)
    assert block["analysis_status"] == "partial" and block["quick_read_status"] == "complete"


@pytest.mark.asyncio
async def test_public_material_change_clears_depth_without_rebuilding_quick_read():
    gateway = Gateway()
    agent = OpenAICommentAgent(gateway, "data")
    first = await agent.analyze_quick_read("公开处理", rows(), public_context_fingerprint="old")
    first["items"][0]["comparisons"] = [{"id": "stale", "text": "旧证据回答"}]
    gateway.calls.clear()
    result = await agent.analyze_quick_read(
        "公开处理", rows(), previous=first, public_context_fingerprint="new"
    )
    assert not gateway.calls
    assert result["items"][0]["summary"] == first["items"][0]["summary"]
    assert not result["items"][0]["comparisons"]


@pytest.mark.asyncio
async def test_independent_relevance_disagreement_is_authoritative_after_stronger_review():
    class Disagreement(Gateway):
        def __init__(self):
            super().__init__()
            self.efforts = []
            self.factory = SimpleNamespace(
                config=lambda role: SimpleNamespace(model="GLM-5.3-FlashX")
            )

        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if "comments" in data and "indices" in data:
                self.efforts.append(kwargs.get("reasoning_effort"))
                return {
                    "classifications": [
                        {"index": s["index"], "relevant": False} for s in data["comments"]
                    ],
                    "decisions": [],
                }
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "samples" in result:
                for s in result["samples"]:
                    s["indices"] = s["indices"][:1]
            return result

    gateway = Disagreement()
    comments = [{**r, "text": "简单的离题内容" + str(i)} for i, r in enumerate(rows())]
    result = await OpenAICommentAgent(gateway, "data").analyze_quick_read("公开处理", comments)
    assert gateway.efforts == ["low", "high"]
    assert result["coverage"]["classified"] == 2
    assert result["coverage"]["irrelevant"] == 2
    assert result["status"] == "complete" and not result["observations"]


@pytest.mark.asyncio
async def test_uncertain_atom_in_compound_comment_does_not_hide_pending_review():
    class PartialReview(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "decisions" in result:
                result["decisions"][0]["accepted"] = None
            return result

    result = await OpenAICommentAgent(PartialReview(), "data").analyze_quick_read(
        "公开处理", rows()
    )
    assert result["coverage"]["index_review_incomplete"] == 1
    assert result["status"] == "partial" and len(result["observations"]) == 3


@pytest.mark.asyncio
async def test_new_unrelated_indices_do_not_regroup_or_regenerate_existing_questions():
    class AppendGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if (
                "comments" in data
                and "indices" not in data
                and data["comments"][0]["text"] == "希望明确复核时限"
            ):
                self.calls.append(prompt)
                return {
                    "samples": [
                        {
                            "index": 0,
                            "relevant": True,
                            "indices": [
                                {"text": "希望明确复核时限", "kind": "question", "stance": "审慎"}
                            ],
                        }
                    ]
                }
            if "existing_questions" in data and data["indices"][0]["kind"] == "question":
                self.calls.append(prompt)
                return {"questions": [{"title": "复核时限是什么？", "indexes": [0]}]}
            return await super().complete_json(role, system, prompt, **kwargs)

    gateway = AppendGateway()
    agent = OpenAICommentAgent(gateway, "data")
    first = await agent.analyze_quick_read("公开处理", rows())
    gateway.calls.clear()
    result = await agent.analyze_quick_read(
        "公开处理",
        rows() + [{**rows()[0], "id": "extra", "text": "希望明确复核时限"}],
        previous=first,
    )
    grouped = [
        json.loads(p.split("\n", 1)[1]) for p in gateway.calls if '"existing_questions"' in p
    ]
    assert len(grouped) == 1 and len(grouped[0]["indices"]) == 1
    assert result["items"][:2] == first["items"]
    assert result["coverage"]["unique"] == 3


@pytest.mark.asyncio
async def test_one_malformed_generated_row_preserves_other_reviewed_rows():
    class BrokenRow(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "samples" in result:
                result["samples"][0]["indices"][0]["kind"] = "invalid"
            return result

    result = await OpenAICommentAgent(BrokenRow(), "data").analyze_quick_read("公开处理", rows())
    assert result["observations"]
    assert any(o["text"] == "要求公开复核程序" for o in result["observations"])
    assert result["coverage"]["samples_with_observations"] == 2


@pytest.mark.asyncio
async def test_grouped_summaries_are_reviewed_together_without_per_question_generation():
    class Bundled(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if "questions" in data:
                self.calls.append(prompt)
                return {
                    "decisions": [
                        {
                            "index": q["index"],
                            "question_accepted": True,
                            "summary_accepted": True,
                            "publicly_verifiable": True,
                        }
                        for q in data["questions"]
                    ]
                }
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "questions" in result:
                for q in result["questions"]:
                    q["summary"] = (
                        "样本认可处理。" if "认可" in q["title"] else "样本要求公开复核程序。"
                    )
            return result

    gateway = Bundled()
    result = await OpenAICommentAgent(gateway, "data").analyze_quick_read("公开处理", rows())
    assert result["status"] == "complete" and len(result["items"]) == 2
    assert sum("questions" in json.loads(p.split("\n", 1)[1]) for p in gateway.calls) == 1
    assert not any('"question":' in p for p in gateway.calls)


@pytest.mark.asyncio
async def test_completed_summary_review_is_retained_when_budget_runs_out():
    class FinalBudget(Gateway):
        done = False

        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if "questions" in data:
                self.done = True
                return {
                    "decisions": [
                        {
                            "index": q["index"],
                            "question_accepted": True,
                            "summary_accepted": True,
                            "publicly_verifiable": True,
                        }
                        for q in data["questions"]
                    ]
                }
            result = await super().complete_json(role, system, prompt, **kwargs)
            for q in result.get("questions", []):
                q["summary"] = "样本认可处理，同时希望公开复核程序。"
            return result

    gateway = FinalBudget()
    result = await OpenAICommentAgent(gateway, "data").analyze_quick_read(
        "公开处理", rows(), can_continue=lambda: asyncio.sleep(0, result=not gateway.done)
    )
    assert len(result["items"]) == 2


@pytest.mark.asyncio
async def test_resume_budget_failure_preserves_previously_approved_questions():
    class ResumeBudget(Gateway):
        done = False

        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if "existing_questions" in json.loads(prompt.split("\n", 1)[1]):
                self.done = True
            return result

    first = await OpenAICommentAgent(Gateway(), "data").analyze_quick_read("公开处理", rows())
    gateway = ResumeBudget()
    result = await OpenAICommentAgent(gateway, "data").analyze_quick_read(
        "公开处理",
        rows() + [{**rows()[0], "id": "extra", "text": "新增诉求：认可处理但希望公开复核程序"}],
        previous=first,
        can_continue=lambda: asyncio.sleep(0, result=not gateway.done),
    )
    assert result["items"] == first["items"]
    assert result["coverage"]["ungrouped_observations"] == 2


def test_ungrouped_approved_content_is_readable_without_opening_details():
    html = _render_block(
        {
            "type": "comment_insight",
            "analysis_version": 5,
            "analysis_mode": "quick_read",
            "title": "评论速读",
            "samples": [],
            "observations": [
                {"id": "O1", "text": "希望公开复核依据", "stance": "质疑", "comment_refs": ["M1"]}
            ],
            "items": [],
        },
        {},
    )
    assert "<details open><summary>查看已审观察及原评论" in html


@pytest.mark.asyncio
async def test_attaching_optional_comments_preserves_core_and_general_scope():
    analysis = await OpenAICommentAgent(Gateway(), "data").analyze_quick_read("公开处理", rows())
    builder = FullReportBuilder.__new__(FullReportBuilder)
    builder.database = SimpleNamespace(
        checkpoint=AsyncMock(return_value={"analysis": analysis}),
        fetch_all=AsyncMock(return_value=[]),
        get_task=AsyncMock(return_value=SimpleNamespace(id="task", investigation_scope="general")),
    )
    builder.scope_reviewer = None
    fixture = Path(__file__).parents[3] / "docs/方案包/fixtures/report-ir-v0.1.fixture.json"
    core = migrate_report(json.loads(fixture.read_text(encoding="utf-8")))
    core.setdefault("quality", {})["release_label"] = "full_report"
    before = copy.deepcopy(core)
    result = await builder.attach_comment_analysis("task", core)
    module = next(b for b in result["blocks"] if b["type"] == "comment_insight")
    assert module["items"] and module["observations"]
    assert result["quality"]["release_label"] == "full_report"
    assert [
        b for b in result["blocks"] if b["type"] not in {"comment_insight", "evidence_appendix"}
    ] == [b for b in before["blocks"] if b["type"] != "evidence_appendix"]
    appendix = next(b for b in result["blocks"] if b["type"] == "evidence_appendix")
    original_appendix = next(b for b in before["blocks"] if b["type"] == "evidence_appendix")
    assert [{k: v for k, v in i.items() if k != "citations"} for i in appendix["items"]] == [
        {k: v for k, v in i.items() if k != "citations"} for i in original_appendix["items"]
    ]
    assert core == before
