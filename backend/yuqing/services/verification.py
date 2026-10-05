from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

Relation = Literal["support", "partial", "contradict", "not_mentioned", "conflict"]
Badge = Literal["verified", "unverified", "disputed", "refuted"]
SourceRole = Literal["authority", "party", "independent", "syndicated", "unknown"]
DropReason = Literal["not_mentioned", "syndicated", "unknown", "party", "unverified"]

# 陈述形如"X 方发布/回应/声明……"时属于归属性陈述：当事方声明可以证实
# "该方作出过此表述"，因此其 party 证据不再剔除（05 §0.4 当事方规则）。
ATTRIBUTION_PATTERN = re.compile(r"^.+(发布|回应|声明|表示|称)")


def is_attribution_claim(text: str) -> bool:
    return bool(ATTRIBUTION_PATTERN.match(text))


def stance_drop_reason(
    *,
    relation: Relation | None,
    source_role: SourceRole,
    attribution_claim: bool,
) -> DropReason | None:
    """该证据行不参与独立信源计数的原因；参与计数时返回 None。

    merge_stances 用它过滤，报告层用它统计——同一份策略只写一次，
    否则"哪些证据被丢弃"会在实现与报告之间漂移，而丢弃本身是静默的。
    """
    if relation is None:
        return "unverified"
    if relation == "not_mentioned":
        return "not_mentioned"
    if source_role in {"syndicated", "unknown"}:
        return source_role
    if source_role == "party" and not attribution_claim:
        return "party"
    return None


@dataclass(frozen=True)
class BadgeInputs:
    ind_s: int
    ind_u: int
    has_conflict: bool
    authority_s: int
    authority_u: int
    verification_complete: bool
    # 是否存在只拿到"部分支持"立场的主体（05-核心契约 §4.3 计数表）。
    # 它不影响徽章颜色，只影响卡片上怎么写结论。
    has_partial: bool = False
    # 仅限主体身份已确认、直接取得原文的发布记录；不适用于实质指控。
    publication_s: int = 0


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
    primary_publication: bool = False


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
    if (
        value.ind_s >= 1
        and value.publication_s >= 1
        and value.ind_u == 0
        and not value.has_conflict
    ):
        return BadgeDecision("verified", "D12", "发布记录已核实（机构原文）")
    if value.ind_s == 1 and value.ind_u == 0 and not value.has_conflict:
        return BadgeDecision(
            "unverified", "D10", "部分支持" if value.has_partial else "单一非权威来源支持"
        )
    return BadgeDecision("unverified", "D11", "部分支持" if value.has_partial else "无有效证据")


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
        if (
            stance_drop_reason(
                relation=item.relation,
                source_role=item.source_role,
                attribution_claim=attribution_claim,
            )
            is not None
        ):
            continue
        grouped[item.publisher_entity or "unknown"].append(item)

    ind_s = ind_u = authority_s = authority_u = publication_s = 0
    has_conflict = False
    has_partial = False
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
        if stance == "partial":
            # partial 不产生支持/反证计数（D11 有意如此），但必须记下来：
            # 否则"材料部分支持"与"无有效证据"在卡片上完全同形。
            has_partial = True
            continue
        # 归并到同一采编主体后，组内可能混有不同 tier/role 的站点（同一主体的官网与
        # 门户号）。只取一条会被证据绑定顺序决定，静默丢掉裁判性权威资格，故按整组判定。
        is_authority = any(
            item.source_role == "authority" and item.source_tier == 1 for item in group
        )
        if stance == "support":
            ind_s += 1
            authority_s += int(is_authority)
            publication_s += int(any(item.primary_publication for item in group))
        elif stance == "contradict":
            ind_u += 1
            authority_u += int(is_authority)

    if any(item.relation in {"contradict", "conflict"} for item in evidence):
        publication_s = 0
    return BadgeInputs(
        ind_s,
        ind_u,
        has_conflict,
        authority_s,
        authority_u,
        verification_complete,
        has_partial,
        publication_s,
    )
