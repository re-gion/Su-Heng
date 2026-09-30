import pytest

from yuqing.services.verifier import (
    UPSTREAM_FAILURE_NOTE,
    ClaimVerifierService,
    VerificationRelation,
    _cited_span,
)
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


def test_cited_span_restores_source_layout_without_changing_characters():
    material = "通报称：\n学校已成立工作组，对整个事件进行调查。"
    assert _cited_span(material, "通报称：学校已成立工作组，对整个事件进行调查。") == material
    assert _cited_span(material, "通报称：学校已成立工作组并完成调查。") is None


@pytest.mark.asyncio
async def test_non_neutral_relation_requires_a_real_citation_and_retries_once(
    runtime_dir, claim_limits
):
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
        ),
        **claim_limits,
    )
    verifier = EmptyThenValidVerifier()

    result = await ClaimVerifierService(database, verifier).verify_claim(claim)

    assert verifier.calls == 2
    assert result.badge == "verified"
    await database.close()


@pytest.mark.asyncio
async def test_social_comment_evidence_never_increases_fact_independent_sources(
    runtime_dir, claim_limits
):
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
        ),
        **claim_limits,
    )

    result = await ClaimVerifierService(database, EmptyThenValidVerifier()).verify_claim(claim)

    assert result.badge == "unverified"
    assert result.independent_sources == 0
    # 评论样本本就不参与独立信源计数，因此也不能把整条 claim 拖成"核验未完成"：
    # 该 claim 的核验是跑完了的，结论是"没有可计数的支持来源"。
    assert result.verification_state == "complete"
    relation = (await database.claim_evidence_rows(claim.pk))[0]
    assert relation["relation"] == "support"
    await database.close()


class RaisingVerifier:
    model_name = "fixture"

    def __init__(self, exc: Exception):
        self.exc = exc

    async def verify(self, claim, evidence):
        raise self.exc


async def _claim_with_one_source(runtime_dir, name: str, limits: dict[str, int]):
    database = Database(runtime_dir / name)
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="核验失败降级"))
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://news.example.com/item",
            title="来源",
            snippet="该陈述得到来源支持。",
            source_role="independent",
            source_tier=2,
        )
    )
    claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="该陈述得到来源支持。",
            agent="fact_investigator",
            evidence_ids=[evidence.local_id],
        ),
        **limits,
    )
    return database, claim


@pytest.mark.asyncio
async def test_upstream_failure_is_labelled_and_still_forces_unverified(runtime_dir, claim_limits):
    database, claim = await _claim_with_one_source(runtime_dir, "verify-upstream.db", claim_limits)

    result = await ClaimVerifierService(
        database, RaisingVerifier(ConnectionError("gateway down"))
    ).verify_claim(claim)

    # 契约要求：绑定证据未全部核验时不得判绿或判红（05 §4.2 R16）。
    assert result.badge == "unverified"
    assert result.verification_state == "incomplete"
    relation = (await database.claim_evidence_rows(claim.pk))[0]
    assert relation["verify_reason"].startswith(UPSTREAM_FAILURE_NOTE)
    await database.close()


@pytest.mark.asyncio
async def test_material_failure_is_labelled_apart_from_upstream_failure(runtime_dir, claim_limits):
    database, claim = await _claim_with_one_source(runtime_dir, "verify-material.db", claim_limits)

    result = await ClaimVerifierService(
        database, RaisingVerifier(ValueError("非 not_mentioned 关系缺少可回溯的 cited_sentence"))
    ).verify_claim(claim)

    assert result.badge == "unverified"
    assert result.verification_state == "incomplete"
    reason = (await database.claim_evidence_rows(claim.pk))[0]["verify_reason"]
    assert reason.startswith("核验失败")
    assert UPSTREAM_FAILURE_NOTE not in reason
    await database.close()
