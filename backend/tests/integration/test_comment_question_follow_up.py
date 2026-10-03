from unittest.mock import AsyncMock

import pytest

from yuqing.agents.runtime import GeneratedClaim
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchResult
from yuqing.core.search.base import SearchResult
from yuqing.services.v1_orchestrator import V1Orchestrator
from yuqing.services.verifier import VerificationRelation
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate
from yuqing.storage.snapshots import SnapshotStore


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_limited", [False, True])
async def test_public_question_flows_through_fetch_claim_and_verification_once(
    runtime_dir, claim_limited
):
    calls = []
    text = "武汉大学发布图书馆事件说明，并说明复核程序。"

    class Search:
        async def search(self, params):
            calls.append("search")
            return [
                SearchResult(
                    url="https://www.whu.edu.cn/notice",
                    title="武汉大学图书馆事件复核说明",
                    snippet=text,
                    lang="zh",
                    provider="fixture",
                )
            ]

    class Fetch:
        async def fetch(self, url):
            calls.append("fetch")
            return FetchResult(
                url=url,
                html=f"<html><body>{text}</body></html>",
                content_text=text,
                content_type="text/html",
            )

    class Agent:
        async def summarize(self, event_query, evidence):
            calls.append("claim")
            return [GeneratedClaim(text=text, evidence_ids=[evidence[0].local_id])]

    class Verifier:
        model_name = "fixture"

        async def verify(self, claim, evidence):
            calls.append("verify")
            return VerificationRelation(relation="support", reason="正文支持", cited_sentence=text)

    db = Database(runtime_dir / "question-follow-up.db")
    await db.initialize()
    try:
        task = await db.create_task(
            TaskCreate(event_query="武汉大学图书馆事件", source_languages=["zh"])
        )
        await db.write(
            lambda conn: conn.execute("UPDATE task SET status='running' WHERE id=?", (task.id,))
        )
        runner = V1Orchestrator(
            db,
            EventBus(db),
            search=Search(),
            fetcher=Fetch(),
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents={"fact_investigator": Agent()},
            moderator=None,
            verifier=Verifier(),
            reports_dir=runtime_dir / "reports",
        )
        question = {
            "id": "Qfixture",
            "title": "图书馆事件的复核程序是什么？",
            "publicly_verifiable": True,
        }
        if claim_limited:
            db.add_claim = AsyncMock(side_effect=ValueError("任务 claim 总数已达当前深度上限 60"))
        result = await runner._comment_follow_up(task.id, task.event_query, question)
        assert (
            result["status"] == ("claim_budget_limited" if claim_limited else "complete")
            and result["evidence"]
        )
        if claim_limited:
            assert not result["evidence"][0]["claims"]
            assert calls == ["search", "fetch", "claim"]
        else:
            assert result["evidence"][0]["claims"][0]["verification_state"] == "complete"
            assert calls == ["search", "fetch", "claim", "verify"]
        await runner._comment_follow_up(task.id, task.event_query, question)
        assert len(calls) == (3 if claim_limited else 4)
    finally:
        await db.close()
