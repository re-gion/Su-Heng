"""Combine repeated descriptions of the same historical event for readers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


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
                merged[key]["claim_refs"] = [raw["claim_ref"]]
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
    return list(merged.values())
