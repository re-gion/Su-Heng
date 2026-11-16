"""Combine repeated descriptions of the same historical event for readers."""

from __future__ import annotations

import re
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any


def historical_identity_issue(case: dict, text: str) -> str | None:
    """Bind a case's named subject to this claim, not any entity on its source page.

    Foreign-language source anchors remain unchanged and must also occur in
    the reviewed claim. A matching translated subject in a display name alone
    cannot establish that it is the same entity as a source-language anchor.
    """

    def compact(value):
        return "".join(str(value or "").casefold().split())

    body, name = compact(text), compact(case.get("name"))
    background = case.get("background") or {}
    subject = compact(background.get("subject")) if isinstance(background, dict) else ""
    generic = {"校方", "该校", "学校", "大学", "学院", "公司", "企业", "相关单位", "监管部门"}
    if not subject or subject in generic or subject not in body:
        institution = compact(case.get("institution"))
        subject = institution if institution and institution in body else ""
    if not subject or subject in generic:
        return "case_identity_unconfirmed"
    at = name.find(subject)
    institution = compact(case.get("institution"))
    if at < 0 and institution and subject.startswith(institution) and name.startswith(institution):
        # A claim can name a more specific role (e.g. the institution's
        # former student) while the case is anchored to the institution.
        at = 0
    if at < 0:
        return "case_subject_mismatch"
    # A university named as the object of an office's interview is not that
    # office's interview event when this claim describes the university's action.
    if re.search(
        r"约谈|调查|督查|处罚|问责|通报|回应|起诉|召回|interview|investigat|sanction", name[:at]
    ):
        return "case_subject_mismatch"
    if institution and institution not in body:
        # A whole page may mention several English institutions. Matching its
        # Chinese case label to a Chinese sentence is not a trusted bilingual
        # identity mapping for a different source-language anchor.
        return "case_subject_alias_unconfirmed"
    return None


def history_relevance(topic: str, text: str) -> int:
    def terms(value: str) -> set[str]:
        chinese = re.findall(r"[\u4e00-\u9fff]+", value)
        return {
            part[index : index + 2] for part in chinese for index in range(len(part) - 1)
        } | set(re.findall(r"[a-z0-9]{2,}", value.casefold()))

    return len(terms(topic) & terms(text))


def history_case_rank(topic: str, card: dict[str, Any]) -> tuple[int, int]:
    title_and_background = " ".join(str(card.get(k) or "") for k in ("event_name", "summary"))
    return history_relevance(topic, title_and_background), len(card.get("evidence_refs", []))


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
        same_fact = next(
            (previous for previous in distinct if _shared_history_fact(card, previous)), None
        )
        if same_fact:
            leads.append(
                {
                    **card,
                    "duplicate_of": same_fact.get("event_name"),
                    "reason": "与已有案例引用相同主体的同一处置事实，但候选事件名或分类不同；保留原记录供身份复核，不重复计入另一正式案例。",
                }
            )
            continue
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


def _shared_history_fact(left: dict, right: dict) -> bool:
    def texts(card):
        return {
            re.sub(r"[\W_]", "", str(fact.get("text") or "")).casefold()
            for fact in card.get("background_facts", [])
            if fact.get("text")
        }

    shared = texts(left) & texts(right)
    if not shared or min(map(len, shared)) < 12:
        return False
    subjects = [
        str(
            (c.get("background_anchors") or {}).get("subject") or c.get("institution") or ""
        ).strip()
        for c in (left, right)
    ]
    return bool(subjects[0] and subjects[0] == subjects[1])


def reconcile_history_card_identity(report: dict[str, Any]) -> None:
    for block in report.get("blocks", []):
        if block.get("type") != "history_compare":
            continue
        distinct, leads = partition_distinct_history_cards(block.get("cards", []))
        block["cards"] = distinct
        if leads:
            block.setdefault("reference_candidates", []).extend(leads)


def reconcile_history_completion(report: dict[str, Any]) -> None:
    """Describe the case background and the comparison that survive report review."""
    status = report.get("quality", {}).get("chapter_status", {}).get("history")
    if not isinstance(status, dict) or status.get("status") not in {"complete", "partial"}:
        return
    blocks = report.get("blocks", [])
    cards = [c for b in blocks if b.get("type") == "history_compare" for c in b.get("cards", [])]
    if not cards:
        status.update(status="missing", message="最终报告未保留通过内容审查的独立对照案例")
        return
    if not all(c.get("background_status") == "complete" for c in cards):
        return
    history_refs = {ref for c in cards for ref in c.get("claim_refs", [])}
    current_refs = {
        item.get("claim_ref")
        for b in blocks
        if b.get("type") == "fact_check_table"
        for item in b.get("items", [])
    }
    compared = any(
        refs & history_refs and refs & current_refs
        for b in blocks
        if b.get("type") == "analysis" and b.get("section") == "06"
        for i in b.get("items", [])
        if (refs := set(i.get("claim_refs", [])))
    )
    if not compared:
        status.update(status="partial", message="独立案例经过可回查，与本案的对照判断尚未通过审查")


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


def _dated_history_passages(card: dict) -> list[tuple[tuple[int, ...], str]]:
    passages = []
    full_date = re.compile(r"(?<!\d)(\d{4})(?:\s*年\s*|-)(\d{1,2})(?:\s*月\s*|-)(\d{1,2})(?:日|\b)")
    for fact in card.get("background_facts", []):
        text = str(fact.get("text") or "")
        dates = list(full_date.finditer(text))
        for index, match in enumerate(dates):
            end = dates[index + 1].start() if index + 1 < len(dates) else len(text)
            passage = re.sub(r"[\W_]", "", text[match.end() : end]).casefold()
            passages.append((tuple(map(int, match.groups())), passage))
    return passages


def _reviewed_history_alias(left: dict, right: dict) -> bool:
    institution = left.get("institution")
    if (
        not institution
        or institution != right.get("institution")
        or any(
            card.get("background_status") != "complete"
            or re.search(r"另一起|另一宗|另案", str(card.get("event_name") or ""))
            for card in (left, right)
        )
    ):
        return False
    if (left.get("case_type") or "analogous") != (right.get("case_type") or "analogous"):
        return False
    meaning = re.compile(r"\d+(?:\.\d+)?|没有|未|不予|不构成|不属于")
    for date, passage in _dated_history_passages(left):
        for other_date, other in _dated_history_passages(right):
            shorter = min(len(passage), len(other))
            if (
                date != other_date
                or shorter < 24
                or meaning.findall(passage) != meaning.findall(other)
            ):
                continue
            match = SequenceMatcher(None, passage, other, autojunk=False)
            longest = match.find_longest_match().size
            if match.ratio() >= 0.86 or (
                longest >= max(20, shorter * 0.65) and match.ratio() >= 0.58
            ):
                return True
    return False


def _reviewed_case_names(cards: list[dict]) -> dict[int, str]:
    groups: list[list[int]] = []
    for index, card in enumerate(cards):
        matching = [
            group for group in groups if any(_reviewed_history_alias(card, cards[i]) for i in group)
        ]
        if not matching:
            groups.append([index])
            continue
        matching[0].append(index)
        for group in matching[1:]:
            matching[0].extend(group)
            groups.remove(group)
    return {
        index: "".join(str(cards[min(group)].get("event_name") or "").casefold().split())
        for group in groups
        if len(group) > 1
        for index in group
    }


def unique_history_cards(
    cards: list[dict[str, Any]], *, merge_reviewed_aliases: bool = False
) -> list[dict[str, Any]]:
    """Keep one card per named case while retaining every cited source."""

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    aliases = _reviewed_case_names(cards) if merge_reviewed_aliases else {}
    for index, raw in enumerate(cards):
        name = aliases.get(index) or "".join(str(raw.get("event_name") or "").casefold().split())
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
        # Keep each source-bound stage instead of silently replacing an earlier
        # announcement with the longest later summary. Never synthesize prose.
        stages = current.get("stages") or [
            {k: deepcopy(v) for k, v in current.items() if k != "stages"}
        ]
        incoming = raw.get("stages") or [{k: deepcopy(v) for k, v in raw.items() if k != "stages"}]
        for stage in incoming:
            if stage not in stages:
                stages.append(deepcopy(stage))
        current["stages"] = stages
        for field in ("summary", "comparison", "outcome"):
            if len(str(raw.get(field) or "")) > len(str(current.get(field) or "")):
                current[field] = raw[field]
        for field in ("evidence_refs", "claim_refs"):
            values = list(current.get(field) or [])
            values.extend(raw.get(field) or [])
            if field == "claim_refs" and raw.get("claim_ref"):
                values.append(raw["claim_ref"])
            current[field] = list(dict.fromkeys(values))
        if len(current["claim_refs"]) > 1:
            # Aggregate citations are bound to all claims on the case. Each stage
            # keeps its own singular claim and only that claim's citations.
            current.pop("claim_ref", None)
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


def group_history_candidates(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold near-named leads for reading without asserting they identify one case."""
    candidates = unique_history_cards(
        [member for card in cards for member in card.get("related_candidates", [card])]
    )
    groups: list[list[dict]] = []

    def identity(card):
        name = str(card.get("event_name") or "").strip()
        institution = str(card.get("institution") or "").strip()
        if not institution:
            match = re.match(r"(.{2,30}?(?:大学|学院|中学|小学|学校|公司|集团|医院))", name)
            institution = match[1] if match else ""
        tail = name.removeprefix(institution)
        tail = re.sub(r"(?:事件|案件|问题|案)$", "", tail)
        return institution, tail

    def close(left, right):
        a, x = identity(left)
        b, y = identity(right)
        if not a or a != b or min(len(x), len(y)) < 4:
            return False
        if left.get("case_type", "analogous") != right.get("case_type", "analogous"):
            return False
        # Explicit different dates or distinct-incident markers beat title similarity.
        dates = [
            re.search(r"20\d{2}(?:[-年]\d{1,2})?", str(c.get("event_time") or ""))
            for c in (left, right)
        ]
        if dates[0] and dates[1] and dates[0][0] != dates[1][0]:
            return False
        if re.search(r"另一起|另一件|第二起|不同事件", x + y):
            return False
        return SequenceMatcher(None, x, y, autojunk=False).ratio() >= 0.8

    for card in candidates:
        group = next((g for g in groups if all(close(card, old) for old in g)), None)
        if group is None:
            groups.append([card])
        else:
            group.append(card)
    result = []
    for group in groups:
        card = deepcopy(group[0])
        if len(group) > 1:
            # An unconfirmed group is only a container. Its original claims and
            # sources are bound within each distinct candidate, not to its label.
            for field in ("claim_ref", "claim_refs", "origin_claim_ref"):
                card.pop(field, None)
            card["related_candidates"] = deepcopy(group)
            card["identity_status"] = "unconfirmed"
            card["evidence_refs"] = list(
                dict.fromkeys(r for c in group for r in c.get("evidence_refs", []))
            )
        result.append(card)
    return result
