"""阅读层证据说明。始终保留原始 badge、正文与独立信源统计。"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from yuqing.core.event_chronology import (
    event_timeline,
    explicit_event_date,
    restore_report_chronology,
)
from yuqing.core.history_cards import reconcile_history_card_identity, reconcile_history_completion
from yuqing.core.timeline_grouping import (
    group_timeline_events,
    publication_fact_key,
    publication_reading_parts,
    same_publication_fact,
)

SOURCE_SUPPORT_STATUSES = {
    "single_source_supported",
    "reposted_source_supported",
    "source_recorded",
    "attributed_source_supported",
}
LABELS = {
    "verified": "已证实",
    "publication_verified": "发布记录已核实",
    "unverified": "待核验",
    "disputed": "有争议",
    "refuted": "已证伪",
    "single_source_supported": "原文直接支持（单源）",
    "reposted_source_supported": "转载材料支持",
    "source_recorded": "来源记载（身份待确认）",
    "attributed_source_supported": "声明原文支持",
    "snippet_supported": "摘要支持（原文未取得）",
    "partially_supported": "部分内容有据",
    "insufficient_evidence": "依据不足",
    "verification_incomplete": "核验未完成",
    "source_conflict": "来源存在分歧",
}

NOTES = {
    "publication_verified": "只确认机构发布记录及文件记载，不证明其中的指控或自述内容属实。",
    "single_source_supported": "原文直接支持整条陈述；独立互证尚不足。",
    "reposted_source_supported": "所引转载材料支持整条陈述；转载不重复计为独立信源，原始发布来源仍需回查。",
    "source_recorded": "已取得的材料记载支持这句话，但发布者身份尚未可靠确认，不能据此认定事实。",
    "attributed_source_supported": "声明原文支持这条表述；声明内容的真实性须另行核验。",
    "snippet_supported": "只有搜索摘要或搜索服务正文支持，尚未取得目标网页原文快照。",
    "partially_supported": "所引材料只支持部分要素，整条陈述仍不能作为已证实事实。",
    "insufficient_evidence": "当前引用未支持整条陈述，需要补充直接证据。",
    "verification_incomplete": "部分绑定材料尚未核验完毕；这不等于材料没有依据。",
    "source_conflict": "引用材料存在反证或内部矛盾，不能按普通来源支持阅读。",
}


def presentation_status(item: dict[str, Any]) -> str:
    badge = item.get("badge") or "unverified"
    if badge != "unverified":
        if badge == "verified" and item.get("verification_basis") == "primary_publication":
            return "publication_verified"
        return str(badge)
    state = item.get("verification_state")
    if state and state != "complete":
        return "verification_incomplete"
    # Older reports without a recorded completion state cannot earn a support label.
    if state != "complete":
        return "unverified"
    citations = [c for c in item.get("citations", []) if c.get("kind") != "social_comments"]
    if any(c.get("relation") in {"contradict", "conflict"} for c in citations):
        return "source_conflict"
    supported = [
        c
        for c in citations
        if c.get("relation") == "support" and c.get("cited_verified") is not False
    ]
    originals = [
        c
        for c in supported
        if c.get("fetch_status") == "fetched"
        or ("fetch_status" not in c and item.get("evidence_grade") == "fulltext")
    ]
    if originals:
        if item.get("independent_sources", 0) >= 1:
            return "single_source_supported"
        roles = {c.get("source_role", "unknown") for c in originals}
        if "party" in roles:
            return "attributed_source_supported"
        if "syndicated" in roles:
            return "reposted_source_supported"
        return "source_recorded"
    if supported:
        return "snippet_supported"
    if any(c.get("relation") == "partial" for c in citations):
        return "partially_supported"
    return "insufficient_evidence"


def presentation_counts(items: list[dict]) -> dict[str, int]:
    return dict(Counter(presentation_status(item) for item in items))


def fact_event_groups(items: list[dict]) -> list[list[dict]]:
    """Use the timeline identity for dated events and retain each original claim."""
    event_by_ref = {}
    publications: dict[tuple, list[tuple[tuple, tuple]]] = {}
    for event in group_timeline_events(event_timeline(items)):
        identity = publication_fact_identity(event)
        parts = publication_reading_parts(event["text"])
        if identity and parts:
            existing = publications.setdefault((event["date"], *parts[:3]), [])
            original = next(
                (key for old, key in existing if same_publication_fact(parts, old)), None
            )
            if original:
                identity = original
            else:
                existing.append((parts, identity))
        for ref in event["claim_refs"]:
            event_by_ref[ref] = identity or ("event", event["claim_ref"])
    dated = [(explicit_event_date(item.get("text") or ""), item) for item in items]
    groups: dict[tuple, list[dict]] = {}
    for index, (date, item) in enumerate(
        sorted(dated, key=lambda pair: (pair[0] is None, pair[0] or ""))
    ):
        ref = item.get("claim_ref")
        text = item.get("text") or ""
        # Without an explicit action date, only identical wording can be folded.
        if ref in event_by_ref:
            key = event_by_ref[ref]
        elif not date and text.strip():
            key = ("undated", text)
        else:
            key = ("claim", index)
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def publication_fact_identity(item: dict) -> tuple | None:
    text = item.get("text") or ""
    parts = publication_reading_parts(text)
    date = explicit_event_date(text)
    if parts and date and parts[3]:
        return ("publication_fact", date, *parts[:3], publication_fact_key(parts[3]))
    return None


def fact_status_key(item: dict) -> tuple:
    """Keep different verdicts and corrections visible even within one event."""
    return (
        presentation_status(item),
        item.get("verification_state"),
        item.get("verdict"),
        item.get("statement_kind"),
        item.get("rumor_text"),
        item.get("correction_text"),
    )


def enrich_report_sources(report: dict[str, Any]) -> dict[str, Any]:
    """Hydrate saved citations from their appendix and align every occurrence by claim ref.

    This is a read-only projection, including for historical reports. Never infer
    a new stored verdict, rewrite a sentence, or migrate publication eligibility.
    """
    value = copy.deepcopy(report)
    sources = {
        item.get("evidence_ref"): item
        for block in value.get("blocks", [])
        if block.get("type") == "evidence_appendix"
        for item in block.get("items", [])
    }
    facts = {}
    for block in value.get("blocks", []):
        if block.get("type") not in {"fact_check_table", "historical_facts"}:
            continue
        for fact in block.get("items", []):
            for citation in fact.get("citations", []):
                source = sources.get(citation.get("evidence_ref"), {})
                for key in (
                    "fetch_status",
                    "source_role",
                    "kind",
                    "source_name",
                    "publisher_entity",
                ):
                    if key in source:
                        citation.setdefault(key, source[key])
            facts[fact.get("claim_ref")] = fact
    restore_report_chronology(value)
    reconcile_history_card_identity(value)
    reconcile_history_completion(value)
    for block in value.get("blocks", []):
        if block.get("chart_kind") not in {"timeline", "matrix"}:
            continue
        for item in block.get("items", []):
            refs = item.get("claim_refs") or [item.get("claim_ref")]
            matches = [facts[ref] for ref in refs if ref in facts]
            if len(matches) == 1 and item.get("text") == matches[0].get("text"):
                for key in (
                    "badge",
                    "verification_state",
                    "verification_basis",
                    "independent_sources",
                    "evidence_grade",
                    "citations",
                ):
                    if key in matches[0]:
                        item[key] = copy.deepcopy(matches[0][key])
    primary = [
        item
        for block in value.get("blocks", [])
        if block.get("type") == "fact_check_table"
        for item in block.get("items", [])
    ]
    counts = presentation_counts(primary)
    source_count = sum(counts.get(key, 0) for key in SOURCE_SUPPORT_STATUSES)
    unresolved = sum(item.get("badge") == "unverified" for item in primary) - source_count
    for block in value.get("blocks", []):
        method = block.get("verification_method")
        if isinstance(method, dict):
            method["source_supported_count"] = source_count
        if block.get("block_id") == "b_00_kpi":
            for item in block.get("items", []):
                if item.get("label") == "来源直接支持":
                    item.update(
                        value=f"{source_count} / {len(primary)}",
                        note="材料支持整条陈述；来源身份、转载和独立互证限制分别说明",
                    )
                elif item.get("label") in {"待核验陈述", "仍需补证陈述"}:
                    item.update(
                        label="仍需补证陈述", value=f"{max(0, unresolved)} / {len(primary)}"
                    )
    return value
