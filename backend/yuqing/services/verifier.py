from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Literal, Protocol

from pydantic import BaseModel

from yuqing.core.claim_semantics import publication_actor
from yuqing.core.llm.gateway import (
    LLMBudgetExhausted,
    is_upstream_failure,
    sanitize_upstream_message,
)
from yuqing.services.source_tiers import bundled_classifier
from yuqing.services.verification import (
    EntityEvidence,
    decide_badge,
    is_attribution_claim,
    merge_stances,
)
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimRecord, EvidenceRecord

# 核验失败的两种成因：上游不可用（换时间重试可能成功）与材料本身无法回溯。
# 两者都按 05 §4.2 R16 强制 unverified，但报告必须让读者看清是哪一种——
# 否则"没核完"会被读成"材料不支持"。
UPSTREAM_FAILURE_NOTE = "核验未完成（上游不可用）"


class VerificationRelation(BaseModel):
    relation: Literal["support", "partial", "contradict", "not_mentioned", "conflict"]
    reason: str
    cited_sentence: str = ""
    is_correction: bool = False


class EvidenceVerifier(Protocol):
    model_name: str

    async def verify(
        self, claim: ClaimRecord, evidence: EvidenceRecord
    ) -> VerificationRelation: ...


def _cited_span(material: str, quote: str) -> str | None:
    """Return the original source span when a model only normalizes layout whitespace."""

    if not quote or len(quote) > 2000:
        return None
    if quote in material:
        return quote
    compact_quote = "".join(char for char in quote if not char.isspace())
    if len(compact_quote) < 8:
        return None
    compact_material = []
    offsets = []
    for index, char in enumerate(material):
        if not char.isspace():
            compact_material.append(char)
            offsets.append(index)
    start = "".join(compact_material).find(compact_quote)
    if start < 0:
        return None
    return material[offsets[start] : offsets[start + len(compact_quote) - 1] + 1]


def primary_publication_source(claim: ClaimRecord, evidence: EvidenceRecord) -> bool:
    """A registered institution's own snapshot can confirm only its publication record."""
    if claim.statement_kind != "fact" or evidence.kind != "web":
        return False
    issuer = bundled_classifier().institution_for(evidence.source_domain)
    return bool(
        issuer
        and publication_actor(claim.text) == issuer
        and evidence.fetch_status == "fetched"
        and evidence.content_text
        and evidence.snapshot_path
        and evidence.content_sha256
    )


class ClaimVerifierService:
    def __init__(self, database: Database, verifier: EvidenceVerifier):
        self.database = database
        self.verifier = verifier

    async def verify_claim(
        self, claim: ClaimRecord, *, reuse_completed: bool = False
    ) -> ClaimRecord:
        rows = await self.database.claim_evidence_rows(claim.pk)
        stances: list[EntityEvidence] = []
        completed = True
        for row in rows:
            evidence_data = dict(row)
            if isinstance(evidence_data.get("extra"), str):
                evidence_data["extra"] = json.loads(evidence_data["extra"])
            evidence = EvidenceRecord.model_validate(evidence_data)
            try:
                cached = (
                    reuse_completed
                    and row["relation"]
                    and not str(row["verify_reason"] or "").startswith(
                        ("核验失败", UPSTREAM_FAILURE_NOTE)
                    )
                )
                if cached:
                    result = VerificationRelation(
                        relation=row["relation"],
                        reason=row["verify_reason"] or "",
                        cited_sentence=row["cited_sentence"] or "",
                        is_correction=bool(row["is_correction"]),
                    )
                    cited_verified = bool(row["cited_verified"])
                gateway = getattr(self.verifier, "gateway", None)
                context = (
                    gateway.logical_call(stage="verification")
                    if hasattr(gateway, "logical_call")
                    else nullcontext()
                )
                with context:
                    for attempt in range(0 if cached else 2):
                        result = await self.verifier.verify(claim, evidence)
                        material = evidence.content_text or evidence.snippet or ""
                        exact_span = _cited_span(material, result.cited_sentence)
                        cited_verified = exact_span is not None
                        if exact_span is not None:
                            result = result.model_copy(update={"cited_sentence": exact_span})
                        if result.relation == "not_mentioned" or cited_verified:
                            break
                        if attempt == 1:
                            raise ValueError("非 not_mentioned 关系缺少可回溯的 cited_sentence")
            except Exception as exc:
                completed = False
                prefix = UPSTREAM_FAILURE_NOTE if is_upstream_failure(exc) else "核验失败"
                await self.database.set_evidence_relation(
                    claim.pk,
                    evidence.pk,
                    relation="not_mentioned",
                    reason=f"{prefix}：{sanitize_upstream_message(exc)}",
                    cited_sentence="",
                    cited_verified=False,
                )
                if isinstance(exc, LLMBudgetExhausted) or is_upstream_failure(exc):
                    break
                continue
            await self.database.set_evidence_relation(
                claim.pk,
                evidence.pk,
                relation=result.relation,
                reason=result.reason,
                cited_sentence=result.cited_sentence,
                cited_verified=cited_verified,
                is_correction=result.is_correction,
            )
            # 评论样本可以校验“样本中出现了该观点”，但不能作为事实性陈述的
            # 独立发布主体计数。即使后续误将评论证据绑定到事实 claim，这里也兜底隔离。
            if evidence.kind != "social_comments":
                stances.append(
                    EntityEvidence(
                        # 读取侧再次归并：历史行的 publisher_entity 可能是裸主机名，
                        # 且主体映射表会变，计数不能依赖写入当时的映射结果。
                        publisher_entity=bundled_classifier().canonical_publisher(
                            evidence.source_domain or "",
                            evidence.publisher_entity,
                        ),
                        source_role="party"
                        if primary_publication_source(claim, evidence)
                        else evidence.source_role,
                        source_tier=evidence.source_tier,
                        relation=result.relation,
                        published_at=evidence.published_at,
                        is_correction=result.is_correction,
                        primary_publication=(
                            primary_publication_source(claim, evidence)
                            and result.relation == "support"
                            and cited_verified
                        ),
                    )
                )

        attribution = is_attribution_claim(claim.text)
        # 评论样本不计入独立信源（上面的 kind 判断），也因而不能出现在
        # "是否核验完毕"的分母里：否则绑定评论的 claim 恒被 R16 判 D1，
        # 与"评论证实不了事实"这件事无关。
        counted_rows = [row for row in rows if row["kind"] != "social_comments"]
        inputs = merge_stances(
            stances,
            verification_complete=completed and len(stances) == len(counted_rows),
            attribution_claim=attribution,
        )
        decision = decide_badge(inputs)
        # 材料关系与独立主体计数是两个维度。转载/未知主体不能制造互证，
        # 但其已回溯的支持关系不能被改写为“未提及”。
        relations = {item.relation for item in stances}
        verdict = (
            "conflict"
            if "conflict" in relations or {"support", "contradict"} <= relations
            else "support"
            if "support" in relations
            else "contradict"
            if "contradict" in relations
            else "partial"
            if "partial" in relations
            else "not_mentioned"
        )
        # 正式证实/证伪结论仍依照归并立场（包含时序更正）；原始材料关系
        # 只补全 unverified 的阅读原因，不能推翻更正或将其判成反向陈述。
        if decision.badge in {"verified", "refuted", "disputed"}:
            verdict = {"verified": "support", "refuted": "contradict", "disputed": "conflict"}[
                decision.badge
            ]
        note = decision.note
        if decision.badge == "unverified" and inputs.verification_complete and inputs.ind_s == 0:
            if verdict == "support":
                note = "材料直接支持，独立发布来源尚未确认"
            elif verdict == "partial":
                note = "部分内容有据，独立发布来源尚未确认"
        independent = (
            max(inputs.ind_s, inputs.ind_u)
            if decision.badge == "disputed"
            else (inputs.ind_u if decision.badge == "refuted" else inputs.ind_s)
        )
        relevant_tiers = [item.source_tier for item in stances if item.relation != "not_mentioned"]
        await self.database.set_claim_verification(
            claim.pk,
            badge=decision.badge,
            verdict=verdict,
            reason=note,
            state="complete" if inputs.verification_complete else "incomplete",
            independent_sources=independent,
            max_source_tier=min(relevant_tiers) if relevant_tiers else None,
            verifier_model=self.verifier.model_name,
        )
        updated = await self.database.get_claim(claim.task_id, claim.local_id)
        assert updated is not None
        return updated
