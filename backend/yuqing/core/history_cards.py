"""Combine repeated descriptions of the same historical event for readers."""

from __future__ import annotations

import re
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any


def _ambiguous_case_identity(left: dict, right: dict) -> bool:
    a = re.search(r"20\d{2}-\d{2}", str(left.get("event_time") or ""))
    b = re.search(r"20\d{2}-\d{2}", str(right.get("event_time") or ""))
    if (
        not a
        or not b
        or a[0] != b[0]
        or left.get("case_type", "analogous") != right.get("case_type", "analogous")
    ):
        return False
    institution = re.compile(r"[\u4e00-\u9fff]{2,36}?(?:大学|学院|中学|小学|学校|公司|集团|医院)")
    generic = {"实验学校", "职业技术学院", "外国语学校", "高级中学", "初级中学", "附属学校"}
    names = [str(card.get("event_name") or "") for card in (left, right)]
    for x in institution.finditer(names[0]):
        for y in institution.finditer(names[1]):
            common = []
            for first, second in zip(reversed(x[0]), reversed(y[0]), strict=False):
                if first != second:
                    break
                common.append(first)
            subject = "".join(reversed(common))
            if len(subject) < 4 or subject in generic:
                continue
            tails = [
                re.sub(r"(?:事件|案件|问题|案)$", "", text[end:])
                for text, end in zip(names, (x.end(), y.end()), strict=True)
            ]
            if (
                min(map(len, tails)) >= 4
                and SequenceMatcher(None, *tails, autojunk=False).ratio() >= 0.72
            ):
                return True
    return False


def partition_distinct_history_cards(cards: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep ambiguous aliases as leads; never merge their facts or assert they are identical."""
    distinct, leads = [], []
    for card in cards:
        if any(_ambiguous_case_identity(card, previous) for previous in distinct):
            leads.append(
                {
                    **card,
                    "reason": "与已有案例共享主体、材料月份且名称相近，尚未证明为另一起独立事件；保留为补充线索，不重复计入正式对照。",
                }
            )
        else:
            distinct.append(card)
    return distinct, leads


def reconcile_history_card_identity(report: dict[str, Any]) -> None:
    for block in report.get("blocks", []):
        if block.get("type") != "history_compare":
            continue
        distinct, leads = partition_distinct_history_cards(block.get("cards", []))
        block["cards"] = distinct
        if leads:
            block.setdefault("reference_candidates", []).extend(leads)


def reconcile_history_completion(report: dict[str, Any]) -> None:
    """A removed comparison chapter cannot retain its earlier completion claim."""
    status = report.get("quality", {}).get("chapter_status", {}).get("history")
    if not isinstance(status, dict) or status.get("status") != "complete":
        return
    if any(b.get("type") == "history_compare" and b.get("cards") for b in report.get("blocks", [])):
        return
    status.update(status="missing", message="最终报告未保留通过内容审查的独立对照案例")


def background_complete(card: dict[str, Any]) -> bool:
    """Require background selections to occur in the supported claim bodies.

    Selections identify the subject, core event and known progress; they are
    metadata, never a second, model-written version of the factual narrative.
    An unresolved outcome does not make the background incomplete.
    """
    facts = card.get("background_facts")
    anchors = card.get("background_anchors") or {}
    if not isinstance(facts, list) or not isinstance(anchors, dict):
        return False
    texts = [str(item.get("text") or "") for item in facts if isinstance(item, dict)]
    selections = [str(anchors.get(key) or "").strip() for key in ("subject", "event", "progress")]
    if selections[1] == selections[0] or re.fullmatch(
        r"(?:涉事|相关)?(?:餐厅|食堂|学校|机构|企业|公司|人员|学生)", selections[1]
    ):
        return False
    return all(value and any(value in text for text in texts) for value in selections) and (
        selections[1] != selections[2]
    )


def unique_history_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one card per named case while retaining every cited source."""

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(cards):
        name = "".join(str(raw.get("event_name") or "").casefold().split())
        # An unnamed legacy card has no reliable case identity.
        key = (str(raw.get("case_type") or "analogous"), name or f"__unnamed_{index}")
        if key not in merged:
            merged[key] = deepcopy(raw)
            if raw.get("claim_ref"):
                merged[key]["claim_refs"] = list(
                    dict.fromkeys([*raw.get("claim_refs", []), raw["claim_ref"]])
                )
            continue
        current = merged[key]
        for field in ("summary", "comparison", "outcome"):
            if len(str(raw.get(field) or "")) > len(str(current.get(field) or "")):
                current[field] = raw[field]
        for field in ("evidence_refs", "claim_refs"):
            values = list(current.get(field) or [])
            values.extend(raw.get(field) or [])
            if field == "claim_refs" and raw.get("claim_ref"):
                values.append(raw["claim_ref"])
            current[field] = list(dict.fromkeys(values))
        for label, value in (raw.get("dimensions") or {}).items():
            if value and not (current.get("dimensions") or {}).get(label):
                current.setdefault("dimensions", {})[label] = value
        for fact in raw.get("background_facts", []):
            if fact not in current.setdefault("background_facts", []):
                current["background_facts"].append(deepcopy(fact))
        for field, value in (raw.get("background_anchors") or {}).items():
            if value and not (current.get("background_anchors") or {}).get(field):
                current.setdefault("background_anchors", {})[field] = value
    return list(merged.values())
