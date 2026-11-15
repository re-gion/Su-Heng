"""Arrange chapter 06 for readers without changing stored IR or review digests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from yuqing.core.history_cards import history_relevance, unique_history_cards


def history_chapter(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    source = [
        block
        for block in blocks
        if block.get("section") == "06"
        and block.get("type") in {"analysis", "history_compare", "historical_facts"}
    ]
    if not source:
        return None
    facts = {
        item["claim_ref"]: item
        for block in source
        if block.get("type") == "historical_facts"
        for item in block.get("items", [])
        if item.get("claim_ref")
    }
    cards = unique_history_cards([card for block in source for card in block.get("cards", [])])
    references = unique_history_cards(
        [card for block in source for card in block.get("reference_candidates", [])]
    )
    identified_refs = {ref for card in [*cards, *references] for ref in card.get("claim_refs", [])}
    for ref, fact in facts.items():
        if ref not in identified_refs:
            references.append(
                {
                    "event_name": f"历史材料 {ref}（案例背景待补充）",
                    "background_facts": [deepcopy(fact)],
                    "claim_refs": [ref],
                    "evidence_refs": [c["evidence_ref"] for c in fact.get("citations", [])],
                    "reason": "旧报告未保留可对应的案例介绍，仅展示已有事实片段；需补齐案例身份与背景后重新生成对照。",
                }
            )
    ready = []
    for card in cards:
        background = [
            deepcopy(facts[ref]) for ref in card.get("claim_refs", []) if ref in facts
        ] or deepcopy(card.get("background_facts", []))
        card["background_facts"] = background
        # Older IR has no completeness selections. Keep its existing prose visible
        # with an explicit boundary; never invent a replacement introduction.
        summary = str(card.get("summary") or card.get("one_liner") or "").strip()
        has_prose = summary and summary not in {
            str(card.get("comparison") or "").strip(),
            str(card.get("event_name") or "").strip(),
        }
        if card.get("background_status") == "incomplete" or not (background or has_prose):
            references.append({**card, "reason": "案例背景待补充，暂不形成正式对照或启示。"})
        else:
            card["analyses"] = []
            ready.append(card)
    history_refs = set(facts) | {
        ref for card in [*cards, *references] for ref in card.get("claim_refs", [])
    }
    joint = []
    unbound = 0
    for block in source:
        if block.get("type") != "analysis":
            continue
        for index, item in enumerate(block.get("items", [])):
            refs = set(item.get("claim_refs", [])) & history_refs
            matches = [card for card in ready if refs & set(card.get("claim_refs", []))]
            covered = {ref for card in matches for ref in card.get("claim_refs", [])}
            if not refs or not refs.issubset(covered):
                unbound += 1
                continue
            analysis = {
                **deepcopy(block),
                "block_id": f"{block.get('block_id', 'history-analysis')}-case-{index}",
                "title": "与本案的对照与启示" if len(matches) == 1 else "综合对照",
                "items": [deepcopy(item)],
                "case_names": [card.get("event_name") for card in matches],
            }
            if len(matches) == 1:
                matches[0]["analyses"].append(analysis)
            else:
                joint.append(analysis)
    return {
        "type": "history_chapter",
        "section": "06",
        # Background must stay visible whenever its analysis is visible in brief view.
        "in_brief": any(block.get("in_brief") for block in source),
        "title": "历史对照",
        "anchors": list(dict.fromkeys(b["block_id"] for b in source if b.get("block_id"))),
        "cards": ready,
        "joint_analyses": joint,
        "reference_candidates": references,
        "excluded_candidates": [item for b in source for item in b.get("excluded_candidates", [])],
        "basis_blocks": [b for b in source if b.get("type") == "historical_facts"],
        "unbound_analyses": unbound,
        "fallback_text": next((b["fallback_text"] for b in source if b.get("fallback_text")), None),
    }


def arrange_history(blocks: list[dict[str, Any]], topic: str = "") -> list[dict[str, Any]]:
    chapter = history_chapter(blocks)
    if chapter is None:
        return blocks
    chapter["cards"].sort(
        key=lambda card: (
            history_relevance(
                topic,
                " ".join(
                    str(card.get(field) or "") for field in ("event_name", "summary", "comparison")
                ),
            ),
            bool(card.get("analyses")),
            len(card.get("evidence_refs", [])),
        ),
        reverse=True,
    )
    result = []
    inserted = False
    for block in blocks:
        if block.get("section") == "06" and block.get("type") in {
            "analysis",
            "history_compare",
            "historical_facts",
        }:
            if not inserted:
                result.append(chapter)
                inserted = True
        else:
            result.append(block)
    return result
