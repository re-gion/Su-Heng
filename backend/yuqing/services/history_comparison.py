"""Gate independent historical cases before they can enter comparison analysis."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any

from yuqing.core.history_cards import historical_identity_issue

_MODEL_IDENTIFIER = re.compile(r"(?<![a-z0-9])[a-z]{1,8}[ -]?\d[a-z0-9]*(?![a-z0-9])")


def same_investigation_event(event_query: str, case_name: str) -> bool:
    def identity(label: str) -> str:
        compact = re.sub(r"[\W_]", "", label)
        return re.sub(r"(?:问题|事件|案件|舆情|争议)+$", "", compact)

    query, title = (
        unicodedata.normalize("NFKC", value).casefold() for value in (event_query, case_name)
    )
    topic, name = identity(query), identity(title)
    if not topic:
        return False
    if topic == name or len(topic) >= 6 and topic in name:
        return True

    def models(value: str) -> set[str]:
        return {re.sub(r"[ -]", "", item) for item in _MODEL_IDENTIFIER.findall(value)}

    # Shared concrete identifiers plus near-identical incident descriptions
    # catch regional spelling and minor heading edits. Similarity alone does
    # not reject an independent case involving another model.
    return bool(
        min(len(topic), len(name)) >= 10
        and models(query) & models(title)
        and SequenceMatcher(None, topic, name, autojunk=False).ratio() >= 0.72
    )


def independent_case_evidence(
    claim: Any,
    evidence_by_id: Mapping[str, Any],
    event_query: str,
    cutoff: str | None,
    *,
    require_confirmed_date: bool = True,
) -> set[str]:
    """Return eligible sources only for an explicitly identified, distinct case.

    A history agent's role or an out-of-window source alone cannot establish a
    different event: the agent must identify its institution and event, and the
    institution must be present in its cited source rather than invented.
    """
    case = (claim.analysis_data or {}).get("historical_case")
    if not isinstance(case, dict) or case.get("independent") is not True:
        return set()
    name = str(case.get("name") or "").strip()
    institution = str(case.get("institution") or "").strip()
    similarity = str(case.get("similarity") or "").strip()
    difference = str(case.get("difference") or "").strip()
    # An unresolved case can still document a comparable response mechanism.
    # A missing outcome is disclosed by the renderer, never inferred.
    if not all((name, institution, similarity, difference)):
        return set()
    text = getattr(claim, "text", None)
    if isinstance(text, str) and historical_identity_issue(case, text):
        return set()
    normal_name = "".join(name.lower().split())
    if same_investigation_event(event_query, name):
        return set()
    case_type = case.get("case_type", "analogous")
    if case_type not in {"analogous", "related_prior"}:
        return set()
    connection_quote = str(case.get("connection_quote") or "").strip()
    if case_type == "related_prior" and not connection_quote:
        return set()
    result = set()
    for ref in claim.evidence_ids:
        source = evidence_by_id.get(ref)
        if source is None or (source.extra or {}).get("scope_status") != "history":
            continue
        trusted_date = (source.extra or {}).get("date_provenance") in {
            "page_metadata",
            "page_visible",
            "trusted_structured",
            "user_provided",
        }
        if require_confirmed_date and (not trusted_date or not source.published_at):
            continue
        if source.published_at and cutoff and str(source.published_at)[:10] > cutoff[:10]:
            continue
        source_text = f"{source.title or ''} {source.content_text or source.snippet or ''}"
        if institution.lower() not in source_text.lower():
            continue
        if institution in event_query and normal_name not in "".join(source_text.lower().split()):
            # Same institution is allowed, but the cited source must identify
            # this different event, rather than only mentioning the institution.
            continue
        if case_type == "related_prior" and (
            source.fetch_status != "fetched" or connection_quote not in (source.content_text or "")
        ):
            continue
        result.add(ref)
    return result
