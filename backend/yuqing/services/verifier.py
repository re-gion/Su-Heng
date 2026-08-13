from __future__ import annotations

import json
import re
from typing import Literal, Protocol

from pydantic import BaseModel

from yuqing.services.verification import EntityEvidence, decide_badge, merge_stances
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimRecord, EvidenceRecord


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


class ClaimVerifierService:
    def __init__(self, database: Database, verifier: EvidenceVerifier):
        self.database = database
        self.verifier = verifier

    async def verify_claim(self, claim: ClaimRecord) -> ClaimRecord:
        rows = await self.database.claim_evidence_rows(claim.pk)
        stances: list[EntityEvidence] = []
        completed = True
        for row in rows:
            evidence_data = dict(row)
            if isinstance(evidence_data.get("extra"), str):
                evidence_data["extra"] = json.loads(evidence_data["extra"])
            evidence = EvidenceRecord.model_validate(evidence_data)
            try:
                for attempt in range(2):
                    result = await self.verifier.verify(claim, evidence)
                    material = evidence.content_text or evidence.snippet or ""
                    cited_verified = bool(result.cited_sentence) and (
                        result.cited_sentence in material
                    )
                    if result.relation == "not_mentioned" or cited_verified:
                        break
                    if attempt == 1:
                        raise ValueError("非 not_mentioned 关系缺少可回溯的 cited_sentence")
            except Exception as exc:
                completed = False
                await self.database.set_evidence_relation(
                    claim.pk,
                    evidence.pk,
                    relation="not_mentioned",
                    reason=f"核验失败：{exc}",
                    cited_sentence="",
                    cited_verified=False,
                )
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
                        publisher_entity=evidence.publisher_entity or evidence.source_domain,
                        source_role=evidence.source_role,
                        source_tier=evidence.source_tier,
                        relation=result.relation,
                        published_at=evidence.published_at,
                        is_correction=result.is_correction,
                    )
                )

        attribution = bool(re.match(r"^.+(发布|回应|声明|表示|称)", claim.text))
        inputs = merge_stances(
            stances,
            verification_complete=completed and len(stances) == len(rows),
            attribution_claim=attribution,
        )
        decision = decide_badge(inputs)
        verdict = (
            "support"
            if inputs.ind_s > 0 and inputs.ind_u == 0
            else (
                "contradict"
                if inputs.ind_u > 0 and inputs.ind_s == 0
                else (
                    "conflict"
                    if inputs.has_conflict or inputs.ind_s + inputs.ind_u > 0
                    else "not_mentioned"
                )
            )
        )
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
            reason=decision.note,
            state="complete" if inputs.verification_complete else "incomplete",
            independent_sources=independent,
            max_source_tier=min(relevant_tiers) if relevant_tiers else None,
            verifier_model=self.verifier.model_name,
        )
        updated = await self.database.get_claim(claim.task_id, claim.local_id)
        assert updated is not None
        return updated
