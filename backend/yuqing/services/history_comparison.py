"""Gate independent historical cases before they can enter comparison analysis."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def independent_case_evidence(
    claim: Any, evidence_by_id: Mapping[str, Any], event_query: str, cutoff: str | None
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
    outcome = str(case.get("outcome") or "").strip()
    if not all((name, institution, similarity, difference, outcome)):
        return set()
    normal_query = "".join(event_query.lower().split())
    normal_name = "".join(name.lower().split())
    if normal_name == normal_query or normal_query in normal_name:
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
        if (source.extra or {}).get("date_provenance") not in {
            "page_metadata",
            "page_visible",
            "trusted_structured",
            "user_provided",
        }:
            continue
        if not source.published_at or (cutoff and str(source.published_at)[:10] > cutoff[:10]):
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
