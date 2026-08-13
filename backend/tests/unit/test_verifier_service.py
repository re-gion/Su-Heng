import pytest

from yuqing.services.verifier import ClaimVerifierService, VerificationRelation
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, TaskCreate


class EmptyThenValidVerifier:
    model_name = "fixture"

    def __init__(self):
        self.calls = 0

    async def verify(self, claim, evidence):
        self.calls += 1
        return VerificationRelation(
            relation="support",
            reason="fixture",
            cited_sentence="" if self.calls == 1 else evidence.snippet,
        )


@pytest.mark.asyncio
async def test_non_neutral_relation_requires_a_real_citation_and_retries_once(runtime_dir):
    database = Database(runtime_dir / "verify.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="引用测试"))
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.com/item",
            title="来源",
            snippet="该陈述得到来源支持。",
            source_role="authority",
            source_tier=1,
        )
    )
    claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="该陈述得到来源支持。",
            agent="fact_investigator",
            evidence_ids=[evidence.local_id],
        )
    )
    verifier = EmptyThenValidVerifier()

    result = await ClaimVerifierService(database, verifier).verify_claim(claim)

    assert verifier.calls == 2
    assert result.badge == "verified"
    await database.close()


@pytest.mark.asyncio
async def test_social_comment_evidence_never_increases_fact_independent_sources(runtime_dir):
    database = Database(runtime_dir / "verify-comments.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="评论不能证实事实"))
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://weibo.com/123456/AbCdEf",
            title="确认帖子的评论样本",
            snippet="样本评论声称该事件成立。",
            kind="social_comments",
            provider="comment_plugin",
        )
    )
    claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="该事件成立。",
            agent="fact_investigator",
            evidence_ids=[evidence.local_id],
        )
    )

    result = await ClaimVerifierService(database, EmptyThenValidVerifier()).verify_claim(claim)

    assert result.badge == "unverified"
    assert result.independent_sources == 0
    relation = (await database.claim_evidence_rows(claim.pk))[0]
    assert relation["relation"] == "support"
    await database.close()
