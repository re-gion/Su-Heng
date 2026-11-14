"""Combine repeated descriptions of the same historical event for readers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


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
