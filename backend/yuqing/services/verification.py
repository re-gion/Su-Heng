from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

Relation = Literal["support", "partial", "contradict", "not_mentioned", "conflict"]
Badge = Literal["verified", "unverified", "disputed", "refuted"]
SourceRole = Literal["authority", "party", "independent", "syndicated", "unknown"]


@dataclass(frozen=True)
class BadgeInputs:
    ind_s: int
    ind_u: int
    has_conflict: bool
    authority_s: int
    authority_u: int
    verification_complete: bool


@dataclass(frozen=True)
class BadgeDecision:
    badge: Badge
    rule: str
    note: str


@dataclass(frozen=True)
class EntityEvidence:
    publisher_entity: str
    source_role: SourceRole
    source_tier: int
    relation: Relation
    published_at: str | None = None
    is_correction: bool = False


def decide_badge(value: BadgeInputs) -> BadgeDecision:
    if not value.verification_complete:
        return BadgeDecision("unverified", "D1", "核验未完成")
    if value.ind_s >= 1 and value.ind_u >= 1:
        return BadgeDecision("disputed", "D2", "支持与反证并存")
    if value.ind_s >= 1 and value.ind_u == 0 and value.has_conflict:
        return BadgeDecision("disputed", "D3", "存在内部矛盾的材料")
    if value.ind_s == 0 and value.ind_u >= 1 and value.authority_u >= 1:
        return BadgeDecision("refuted", "D4", "官方材料否定")
    if value.ind_s == 0 and value.ind_u >= 2 and value.authority_u == 0:
        return BadgeDecision("refuted", "D5", "多个独立信源否定")
    if value.ind_s == 0 and value.ind_u == 1 and value.authority_u == 0:
        return BadgeDecision("unverified", "D6", "单一来源否定，未达证伪门槛")
    if value.ind_s >= 2 and value.ind_u == 0 and not value.has_conflict:
        return BadgeDecision("verified", "D7", "多个独立信源支持")
    if value.ind_s == 1 and value.ind_u == 0 and not value.has_conflict and value.authority_s == 1:
        return BadgeDecision("verified", "D8", "单源·官方")
    if value.ind_s == 0 and value.ind_u == 0 and value.has_conflict:
        return BadgeDecision("disputed", "D9", "材料内部矛盾")
    if value.ind_s == 1 and value.ind_u == 0 and not value.has_conflict:
        return BadgeDecision("unverified", "D10", "单一非权威来源支持")
    return BadgeDecision("unverified", "D11", "无有效证据")


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _newer_correction(group: list[EntityEvidence]) -> Relation | None:
    directional = [item for item in group if item.relation in {"support", "contradict"}]
    if len(directional) < 2 or any(item.source_role != "authority" for item in directional):
        return None
    ordered = sorted(
        directional,
        key=lambda item: _parse_time(item.published_at) or datetime.min.replace(tzinfo=UTC),
    )
    previous, latest = ordered[-2], ordered[-1]
    previous_at, latest_at = _parse_time(previous.published_at), _parse_time(latest.published_at)
    if previous_at is None or latest_at is None or latest_at - previous_at < timedelta(hours=24):
        return None
    if not latest.is_correction or previous.relation == latest.relation:
        return None
    return latest.relation


def merge_stances(
    evidence: list[EntityEvidence],
    *,
    verification_complete: bool,
    attribution_claim: bool = False,
) -> BadgeInputs:
    grouped: dict[str, list[EntityEvidence]] = defaultdict(list)
    for item in evidence:
        if item.relation == "not_mentioned":
            continue
        if item.source_role in {"syndicated", "unknown"}:
            continue
        if item.source_role == "party" and not attribution_claim:
            continue
        grouped[item.publisher_entity or "unknown"].append(item)

    ind_s = ind_u = authority_s = authority_u = 0
    has_conflict = False
    for group in grouped.values():
        if any(item.relation == "conflict" for item in group):
            has_conflict = True
        relations = {item.relation for item in group}
        stance: Relation | Literal["self_conflict"] | None
        if "support" in relations and "contradict" in relations:
            stance = _newer_correction(group) or "self_conflict"
        elif "support" in relations:
            stance = "support"
        elif "contradict" in relations:
            stance = "contradict"
        elif "partial" in relations:
            stance = "partial"
        else:
            stance = None
        if stance == "self_conflict":
            has_conflict = True
            continue
        representative = group[-1]
        is_authority = representative.source_role == "authority" and representative.source_tier == 1
        if stance == "support":
            ind_s += 1
            authority_s += int(is_authority)
        elif stance == "contradict":
            ind_u += 1
            authority_u += int(is_authority)

    return BadgeInputs(ind_s, ind_u, has_conflict, authority_s, authority_u, verification_complete)
