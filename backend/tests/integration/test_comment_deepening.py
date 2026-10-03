import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuqing.core.comment_contract import reconcile_questions
from yuqing.core.llm.gateway import LLMBudgetExhausted
from yuqing.render.ir_migrations import migrate_report
from yuqing.services.comment_deepening import deepen_comment_question
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate


async def fixture_runner(runtime_dir):
    db = Database(runtime_dir / "deep.db")
    await db.initialize()
    task = await db.create_task(TaskCreate(event_query="公共处理程序"))
    await db.set_task_status(task.id, "done", "finished")
    fixture = Path(__file__).parents[3] / "docs/方案包/fixtures/report-ir-v0.1.fixture.json"
    report = migrate_report(json.loads(fixture.read_text(encoding="utf-8")))
    samples = [
        {
            "id": "M1",
            "text": "请说明程序",
            "platform": "weibo",
            "source_url": "https://example.test/post",
            "evidence_ref": "E001",
        }
    ]
    observations = [
        {
            "id": "O1",
            "text": "要求说明程序",
            "kind": "request",
            "stance": "质疑",
            "comment_refs": ["M1"],
            "evidence_refs": ["E001"],
            "review_status": "accepted",
        }
    ]
    question = {
        "id": "Q1",
        "title": "程序依据是什么？",
        "summary": "样本希望说明程序。",
        "summary_review_status": "accepted",
        "observation_refs": ["O1"],
        "evidence_refs": ["E001"],
        "review_status": "accepted",
        "publicly_verifiable": True,
        "followup_value": "low",
        "comparisons": [],
        "judgements": [],
        "component_status": {"comparison": "not_requested", "judgement": "not_requested"},
    }
    block = {
        "type": "comment_insight",
        "block_id": "comments",
        "section": "05",
        "analysis_version": 5,
        "analysis_mode": "quick_read",
        "samples": samples,
        "observations": observations,
        "items": [question],
        "coverage": {"collected": 1, "unique": 1, "classified": 1},
        "priority_order": [],
        "stages": {},
    }
    reconcile_questions(block)
    report["blocks"].append(block)
    await db.save_report(task.id, "r1", report, str(runtime_dir / "report.html"), {})
    await db.save_checkpoint(
        task.id, "comments:analysis", {"analysis": {"run_key": "keep", **copy.deepcopy(block)}}
    )

    async def deepen(event, q, *args, **kwargs):
        return {
            **q,
            "comparisons": [
                {
                    "id": "temporaryC",
                    "status": "partial",
                    "text": "已有材料说明部分程序，仍有具体缺口。",
                    "evidence_refs": ["E001"],
                    "review_status": "accepted",
                }
            ],
            "component_status": {"comparison": "complete", "judgement": "incomplete"},
        }, []

    runner = SimpleNamespace(
        database=db,
        events=SimpleNamespace(emit=AsyncMock()),
        scope_reviewer=None,
        reports=SimpleNamespace(reports_dir=runtime_dir),
        _bind_task_runtime=AsyncMock(),
        _llm_phase_has_room=lambda n: True,
        _comment_public_context=AsyncMock(
            return_value=[{"evidence_ref": "E001", "excerpt": "公开材料"}]
        ),
        _comment_follow_up=AsyncMock(),
        comment_agent=SimpleNamespace(
            system_prompt="source-bound", deepen_question=AsyncMock(side_effect=deepen)
        ),
    )
    return runner, task.id, report


@pytest.mark.asyncio
async def test_selected_deepening_preserves_core_id_and_updates_resume_cache(runtime_dir):
    runner, task, original = await fixture_runner(runtime_dir)
    try:
        assert (await deepen_comment_question(runner, task, "Q1", follow_up=True))[
            "status"
        ] == "not_applicable"
        runner._bind_task_runtime.assert_not_called()
        result = await deepen_comment_question(runner, task, "Q1")
        assert result["status"] == "complete"
        saved = json.loads((await runner.database.get_report_for_task(task))["ir_json"])
        assert saved["blocks"][:-1] == original["blocks"][:-1]
        q = saved["blocks"][-1]["items"][0]
        assert q["id"] == "Q1" and q["comparisons"][0]["id"] == "Q1C"
        assert q["summary"] == original["blocks"][-1]["items"][0]["summary"]
        checkpoint = await runner.database.checkpoint(task, "comments:analysis")
        assert checkpoint["analysis"]["run_key"] == "keep"
        assert checkpoint["analysis"]["items"][0]["comparisons"]
        assert (await deepen_comment_question(runner, task, "Q1"))["reused"]
        runner._llm_phase_has_room = lambda n: False
        assert (await deepen_comment_question(runner, task, "Q1"))["reused"]
        runner._llm_phase_has_room = lambda n: True
        assert runner.comment_agent.deepen_question.await_count == 1
        runner._comment_follow_up.assert_not_called()
        runner._comment_public_context.return_value.append(
            {"evidence_ref": "E002", "excerpt": "补充材料"}
        )
        await deepen_comment_question(runner, task, "Q1")
        assert runner.comment_agent.deepen_question.await_count == 2
    finally:
        await runner.database.close()


@pytest.mark.asyncio
async def test_deepening_budget_failure_keeps_published_report(runtime_dir):
    runner, task, original = await fixture_runner(runtime_dir)
    try:
        runner.comment_agent.deepen_question.side_effect = LLMBudgetExhausted(diagnostic={})
        result = await deepen_comment_question(runner, task, "Q1")
        assert result["status"] == "budget_limited"
        assert json.loads((await runner.database.get_report_for_task(task))["ir_json"]) == original
        assert (await runner.database.checkpoint(task, "comments:deep:Q1"))[
            "status"
        ] == "budget_limited"
        runner._llm_phase_has_room = lambda n: False
        await deepen_comment_question(runner, task, "Q1")
        assert runner.comment_agent.deepen_question.await_count == 1
    finally:
        await runner.database.close()
