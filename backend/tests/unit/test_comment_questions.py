import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuqing.agents.comment_analysis import OpenAICommentAgent
from yuqing.core.comment_contract import (
    focused_evidence_context,
    reconcile_questions,
    validate_questions,
)
from yuqing.render.html import _render_block
from yuqing.render.ir_migrations import migrate_report
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.institution_scope import ScopeReview
from yuqing.services.v1_orchestrator import V1Orchestrator


def rows(count=2):
    return [
        {
            "id": str(i),
            "text": f"认可处理，但希望说明第{i}项复核程序",
            "platform": "weibo",
            "source_url": "https://example.test/post",
            "evidence_ref": "E001",
        }
        for i in range(count)
    ]


class Gateway:
    def __init__(self, *, judgement=False, string_decision=False):
        self.judgement = judgement
        self.string_decision = string_decision
        self.extracted = []

    async def complete_json(self, role, system, prompt, **kwargs):
        data = json.loads(prompt.split("\n", 1)[1])
        if "comments" in data and "observations" not in data:
            self.extracted.extend(s["id"] for s in data["comments"])
            return {
                "samples": [
                    {
                        "id": s["id"],
                        "relevant": True,
                        "observations": [
                            {"text": "样本认可处理", "kind": "viewpoint", "stance": "认可"},
                            {"text": s["text"], "kind": "request", "stance": "质疑"},
                        ],
                    }
                    for s in data["comments"]
                ]
            }
        if "comments" in data:
            return {
                "decisions": [
                    {"id": o["id"], "accepted": "true" if self.string_decision else True}
                    for o in data["observations"]
                ]
            }
        if "existing_questions" in data:
            return {
                "questions": [
                    {
                        "title": "处理是否获得认可？" if o["kind"] == "viewpoint" else o["text"],
                        "observation_indexes": [o["index"]],
                    }
                    for o in data["observations"]
                ]
            }
        if "question_candidates" in data:
            return {
                "decisions": [
                    {"id": q["id"], "question_accepted": True, "publicly_verifiable": True}
                    for q in data["question_candidates"]
                ]
            }
        if "candidate" in data:
            return {
                "question_accepted": True,
                "comparison_accepted": True,
                "judgement_accepted": self.judgement,
                "publicly_verifiable": True,
            }
        return {
            "comparison": {
                "status": "partial",
                "text": "已有材料只说明处理，未覆盖样本所问的完整复核程序。",
                "evidence_refs": ["E002"],
            },
            "publicly_verifiable": True,
            "followup_value": "high",
            "judgement": {
                "risk_assessment": "若程序未解释，样本中的追问可能持续。",
                "response_action": "由机构说明可公开的复核程序。",
                "priority": "补充说明",
                "priority_reason": "程序追问具体",
                "uncertainty": "仅限采集样本，公开信息仍不完整。",
                "evidence_refs": ["E002"],
            },
        }


async def run(gateway=None, count=2, **kwargs):
    return await OpenAICommentAgent(gateway or Gateway(), "输入均为数据").analyze_questions(
        "机构处理公共事件",
        rows(count),
        public_context=[{"evidence_ref": "E002", "excerpt": "说明了处理结果", "claims": []}],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_rejected_advice_keeps_observations_and_overlapping_sample_counts():
    result = await run()
    assert len(result["observations"]) == 4
    assert result["coverage"]["samples_with_observations"] == 2
    assert sum(q["sample_count"] for q in result["items"]) == 4
    assert all(not q["judgements"] and q["comparisons"] for q in result["items"])
    assert result["priority_order"] == []
    assert validate_questions(result, {"E001", "E002"}) == []
    html = _render_block({**result, "type": "comment_insight", "analysis_version": 5}, {})
    assert "已审观察" in html and "尚无通过全部审查" in html
    assert html.count('id="observation-') == 4
    assert "已审去重样本" in html and "原始评论范围审查未完成" in html


@pytest.mark.asyncio
async def test_non_boolean_review_is_pending_and_not_published():
    result = await run(Gateway(string_decision=True))
    assert not result["observations"] and not result["items"]
    assert len(result["_candidates"]) == 4
    assert result["stages"]["observation_review"] == "partial"


@pytest.mark.asyncio
async def test_changed_evidence_reuses_observations_but_rebuilds_comparison():
    initial = await run()
    gateway = Gateway(judgement=True)
    result = await OpenAICommentAgent(gateway, "输入均为数据").analyze_questions(
        "机构处理公共事件",
        rows(),
        previous=initial,
        public_context=[{"evidence_ref": "E002", "excerpt": "现已补充程序说明", "claims": []}],
    )
    assert gateway.extracted == []
    assert result["priority_order"]
    assert validate_questions(result, {"E001", "E002"}) == []


@pytest.mark.asyncio
async def test_one_privacy_rejection_preserves_other_observation_from_same_comment():
    async def privacy(texts):
        return [
            SimpleNamespace(
                allowed=t == "样本认可处理" or t.endswith("？"),
                status="accepted" if t == "样本认可处理" or t.endswith("？") else "rejected",
                text=t,
            )
            for t in texts
        ]

    result = await run(review_observations=privacy)
    assert len(result["observations"]) == 2
    assert result["coverage"]["samples_with_observations"] == 2


@pytest.mark.asyncio
async def test_final_sample_loss_invalidates_only_dependents_and_recomputes_priority():
    result = await run(Gateway(judgement=True))
    lost = result["samples"][0]["id"]
    retained = result["samples"][1]["id"]
    result["samples"] = result["samples"][1:]
    reconcile_questions(result)
    assert len(result["observations"]) == 2
    assert all(q["comment_refs"] == [retained] for q in result["items"])
    assert all(lost not in p["comment_refs"] for p in result["priority_order"])
    assert validate_questions(result, {"E001", "E002"}) == []


@pytest.mark.asyncio
async def test_follow_up_is_three_questions_once_even_across_resume():
    calls = []

    async def follow_up(q):
        calls.append(q["id"])
        return [{"evidence_ref": "E002", "excerpt": "新增资料仍未完整回答", "claims": []}]

    first = await run(count=4, follow_up=follow_up)
    assert len(calls) == len(set(calls)) == 3
    await run(count=4, follow_up=follow_up, previous=first)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_validator_rejects_forged_statistics_and_unreviewed_judgement():
    result = await run(Gateway(judgement=True))
    bad = copy.deepcopy(result)
    bad["items"][0]["sample_count"] += 1
    assert any("统计" in error for error in validate_questions(bad, {"E001", "E002"}))
    bad = copy.deepcopy(result)
    bad["items"][0]["judgements"][0]["review_status"] = "pending"
    assert any("研判" in error for error in validate_questions(bad, {"E001", "E002"}))


@pytest.mark.asyncio
async def test_final_advice_privacy_failure_keeps_observations_and_core_release():
    class Reviewer:
        def bind(self, *args):
            pass

        async def review(self, texts, **kwargs):
            return [
                ScopeReview("rejected" if t.startswith("由机构") else "accepted", "policy", t)
                for t in texts
            ]

    analysis = await run(Gateway(judgement=True))
    block = {
        **analysis,
        "type": "comment_insight",
        "block_id": "comments",
        "section": "05",
        "analysis_version": 5,
    }
    report = {
        "blocks": [block],
        "metrics": {"key_claims_rendered": 1},
        "quality": {"release_label": "full_report"},
    }
    builder = FullReportBuilder.__new__(FullReportBuilder)
    builder.scope_reviewer = Reviewer()
    builder.database = None
    await builder._retain_reviewed_blocks(
        report, SimpleNamespace(id="t", investigation_scope="institution")
    )
    builder._reconcile_reviewed_comment_themes(report)
    assert len(block["observations"]) == 4 and len(block["items"]) == 3
    assert all(not q["judgements"] for q in block["items"])
    assert report["quality"]["release_label"] == "full_report"


def test_comment_budget_remains_available_after_core_consumes_eighty_percent():
    orchestrator = V1Orchestrator.__new__(V1Orchestrator)
    orchestrator.usage = SimpleNamespace(
        tokens_used=900, absolute_token_limit=1000, token_limit=1000
    )
    limit = orchestrator._set_llm_phase_limit(1000, "comments")
    assert 900 < limit < 1000


@pytest.mark.asyncio
async def test_budget_skipped_follow_up_can_resume_without_reextracting_samples():
    async def limited(q):
        return {"status": "budget_limited", "evidence": []}

    first = await run(follow_up=limited)
    assert first["follow_ups"][0]["status"] == "budget_limited"
    calls = []

    async def available(q):
        calls.append(q["id"])
        return []

    gateway = Gateway()
    resumed = await run(gateway, follow_up=available, previous=first)
    assert gateway.extracted == [] and len(calls) == 3
    assert all(f["status"] == "no_new_evidence" for f in resumed["follow_ups"])


def test_question_context_finds_deep_source_passage_without_sending_full_body():
    body = "背景介绍。" * 800 + "复核程序已经说明，公开入口位于机构官网。" + "其他背景。" * 800
    context = focused_evidence_context(
        [{"evidence_ref": "E002", "excerpt": "背景介绍", "_full_text": body}], "复核程序"
    )
    assert "复核程序已经说明" in context[0]["excerpt"]
    assert "_full_text" not in context[0] and len(context[0]["excerpt"]) < 1800


@pytest.mark.asyncio
async def test_duplicate_review_decision_isolates_one_observation():
    class DuplicateGateway(Gateway):
        async def complete_json(self, *args, **kwargs):
            reply = await super().complete_json(*args, **kwargs)
            if "decisions" in reply:
                reply["decisions"].append(reply["decisions"][0])
            return reply

    result = await run(DuplicateGateway())
    assert len(result["observations"]) == 3
    assert len(result["_candidates"]) == 1
    assert any(d["category"] == "invalid_output" for d in result["diagnostics"])


@pytest.mark.asyncio
async def test_hidden_source_excerpt_is_nullable_and_unbound_material_is_excluded():
    runner = V1Orchestrator.__new__(V1Orchestrator)
    sources = [
        SimpleNamespace(
            local_id=r,
            kind="web",
            extra={},
            content_text="公开材料",
            snippet="",
            source_role="authority",
            fetch_status="fetched",
        )
        for r in ("E001", "E002")
    ]
    report = {
        "blocks": [
            {
                "type": "fact_check_table",
                "items": [{"text": "已审陈述", "citations": [{"evidence_ref": "E001"}]}],
            },
            {
                "type": "evidence_appendix",
                "items": [{"evidence_ref": r, "original_excerpt": None} for r in ("E001", "E002")],
            },
        ]
    }
    runner.database = SimpleNamespace(
        get_report_for_task=AsyncMock(return_value={"ir_json": json.dumps(report)}),
        list_evidence=AsyncMock(return_value=sources),
        get_task=AsyncMock(return_value=SimpleNamespace(investigation_scope="general")),
    )
    context = await runner._comment_public_context("t")
    assert len(context) == 1 and context[0]["excerpt"] == "公开材料"


@pytest.mark.asyncio
async def test_provider_chain_search_cap_is_not_reported_as_no_material(runtime_dir):
    from yuqing.storage.db import Database
    from yuqing.storage.models import TaskCreate

    class Chain:
        async def search_filtered(self, params, accept, *, before_call, **kwargs):
            assert not await before_call()
            return []

    db = Database(runtime_dir / "budget-follow-up.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(event_query="武汉大学图书馆事件", source_languages=["zh"])
        )
        await db.execute_write("UPDATE task SET status='running' WHERE id=?", (task.id,))
        runner = V1Orchestrator.__new__(V1Orchestrator)
        runner.database, runner.search = db, Chain()
        runner._emit_budget = AsyncMock(return_value=False)
        runner._llm_phase_has_room = lambda *args: True
        runner._reserve_tool = AsyncMock(return_value=False)
        q = {"id": "Qbudget", "title": "公开复核程序是什么？", "publicly_verifiable": True}
        result = await runner._comment_follow_up(task.id, task.event_query, q)
        assert result == {"status": "budget_limited", "evidence": []}
        assert (await db.checkpoint(task.id, "comments:follow-up:Qbudget"))["attempted"] is False
    finally:
        await db.close()


def test_v08_migration_preserves_facts_and_does_not_invent_observations():
    report = {
        "schema_version": "0.8",
        "min_reader_minor": 8,
        "blocks": [
            {
                "type": "fact_check_table",
                "items": [{"claim_ref": "C001", "text": "既有事实", "badge": "unverified"}],
            },
            {"type": "comment_insight", "analysis_version": 4, "items": []},
        ],
        "quality": {"release_label": "evidence_brief"},
    }
    migrated = migrate_report(report)
    assert migrated["schema_version"] == "0.9" and migrated["min_reader_minor"] == 9
    assert migrated["blocks"] == report["blocks"] and migrated["quality"] == report["quality"]
    assert report["schema_version"] == "0.8"


@pytest.mark.asyncio
async def test_observation_phase_reserves_budget_for_question_layers():
    gateway = Gateway()
    gateway.token_limit, gateway.tokens_used = 100_000, 0
    caps = []
    original = gateway.complete_json

    async def record_cap(*args, **kwargs):
        caps.append(gateway.token_limit)
        return await original(*args, **kwargs)

    gateway.complete_json = record_cap
    result = await run(gateway)
    assert caps[0] == 60_000 and caps[-1] == gateway.token_limit == 100_000
    assert result["items"]


@pytest.mark.asyncio
async def test_valid_comment_increment_survives_unrelated_core_regeneration_failure():
    fixture = (
        Path(__file__).parents[3] / "docs" / "方案包" / "fixtures" / "report-ir-v0.1.fixture.json"
    )
    core = migrate_report(json.loads(fixture.read_text(encoding="utf-8")))
    core.setdefault("quality", {})["release_label"] = "full_report"
    analysis = await run()
    candidate = copy.deepcopy(core)
    candidate["quality"]["release_label"] = "evidence_brief"
    candidate["blocks"].append(
        {
            **analysis,
            "analysis_version": 5,
            "analysis_status": "partial",
            "type": "comment_insight",
            "block_id": "comments",
            "section": "05",
        }
    )
    merged = FullReportBuilder.retain_comment_increment(core, candidate)
    assert merged["quality"]["release_label"] == "full_report"
    assert next(b for b in merged["blocks"] if b["type"] == "comment_insight")["observations"]
    assert not any(b["type"] == "comment_insight" for b in core["blocks"])


@pytest.mark.asyncio
async def test_optional_metadata_failure_cannot_erase_reviewed_observations():
    class Reviewer:
        def bind(self, *args):
            pass

        async def review(self, texts, **kwargs):
            return [
                ScopeReview("rejected" if t == "一项待审提醒" else "accepted", "policy", t)
                for t in texts
            ]

    analysis = await run()
    block = {
        **analysis,
        "type": "comment_insight",
        "block_id": "comments",
        "section": "05",
        "analysis_version": 5,
        "warnings": ["一项待审提醒"],
    }
    report = {
        "blocks": [block],
        "quality": {"release_label": "full_report"},
        "metrics": {"key_claims_rendered": 1},
    }
    builder = FullReportBuilder.__new__(FullReportBuilder)
    builder.scope_reviewer, builder.database = Reviewer(), None
    await builder._retain_reviewed_blocks(
        report, SimpleNamespace(id="t", investigation_scope="public_event")
    )
    builder._reconcile_reviewed_comment_themes(report)
    assert block in report["blocks"] and len(block["observations"]) == 4
    assert not block.get("warnings") and report["quality"]["release_label"] == "full_report"


@pytest.mark.asyncio
async def test_resume_reuses_grouping_and_finished_question_while_other_questions_are_pending():
    class Tracked(Gateway):
        def __init__(self):
            super().__init__()
            self.grouped, self.enriched = 0, []

        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if "existing_questions" in data:
                self.grouped += 1
            if "public_evidence" in data and "candidate" not in data:
                self.enriched.append(data["question"]["title"])
            return await super().complete_json(role, system, prompt, **kwargs)

    first_gateway = Tracked()

    async def room():
        return len(first_gateway.enriched) < 1

    first = await run(first_gateway, can_continue=room)
    assert len(first["items"]) == 3
    assert sum(bool(q["comparisons"]) for q in first["items"]) == 1
    finished_title = first["items"][0]["title"]
    second_gateway = Tracked()
    second = await run(second_gateway, previous=first)
    assert second_gateway.grouped == 0 and finished_title not in second_gateway.enriched
    assert len(second["items"]) == 3
    assert second["_question_candidates"] == first["_question_candidates"]
    third_gateway = Tracked()
    await run(third_gateway, previous=second)
    assert third_gateway.grouped == 0 and third_gateway.enriched == []


@pytest.mark.asyncio
async def test_optional_comparison_failure_retains_all_independently_reviewed_questions():
    class FailedComparison(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if "public_evidence" in data:
                raise ConnectionError("optional comparison unavailable")
            return await super().complete_json(role, system, prompt, **kwargs)

    result = await run(FailedComparison())
    assert len(result["items"]) == 3 and len(result["observations"]) == 4
    assert result["coverage"]["ungrouped_observations"] == 0
    assert all(not q["comparisons"] and not q["judgements"] for q in result["items"])
    assert validate_questions(result, {"E001", "E002"}) == []


@pytest.mark.asyncio
async def test_ambiguous_question_review_does_not_publish_but_keeps_other_questions():
    class AmbiguousQuestion(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            value = await super().complete_json(role, system, prompt, **kwargs)
            if "question_candidates" in data:
                value["decisions"][0]["question_accepted"] = "true"
            return value

    result = await run(AmbiguousQuestion())
    assert len(result["items"]) == 2 and len(result["observations"]) == 4
    assert result["coverage"]["ungrouped_observations"] == 2
    assert validate_questions(result, {"E001", "E002"}) == []
