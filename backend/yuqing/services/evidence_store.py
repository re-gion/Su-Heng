from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import urlsplit

from yuqing.core.fetch.base import FetchProvider
from yuqing.core.search.base import SearchResult
from yuqing.storage.db import Database
from yuqing.storage.models import EvidenceCreate, EvidenceRecord
from yuqing.storage.snapshots import SnapshotStore

from .investigation_scope import InvestigationScope, ScopePhase
from .source_tiers import SourceTierClassifier


def extract_page_published_at(raw_html: str, content_text: str) -> tuple[str | None, str | None]:
    """Extract only dates visibly carried by the fetched page or its structured metadata."""

    decoded = html.unescape(raw_html)
    structured_patterns = (
        r'["\']datePublished["\']\s*:\s*["\']([^"\']+)',
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished|pubdate|publishdate)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:article:published_time|datePublished|pubdate|publishdate)["\']',
    )
    for pattern in structured_patterns:
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            normalized = _normalize_page_date(match.group(1))
            if normalized:
                return normalized, "page_metadata"
    visible = re.search(
        r"(?:发布时间|发布日期|发布于|时间)\s*[：:]?\s*(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})日?",
        content_text[:12000],
    )
    if visible:
        normalized = _normalize_page_date("-".join(visible.groups()))
        if normalized:
            return normalized, "page_visible"
    return None, None


def _normalize_page_date(value: str) -> str | None:
    compact = value.strip().replace("年", "-").replace("月", "-").replace("日", "")
    compact = compact.replace("/", "-").replace(".", "-")
    try:
        parsed = datetime.fromisoformat(compact.replace("Z", "+00:00"))
    except ValueError:
        match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", compact)
        if not match:
            return None
        try:
            year, month, day = (int(item) for item in match.groups())
            parsed = datetime(year, month, day)
        except ValueError:
            return None
    return parsed.isoformat()


class EvidenceStore:
    def __init__(
        self,
        database: Database,
        snapshots: SnapshotStore,
        fetcher: FetchProvider,
        classifier: SourceTierClassifier | None = None,
    ):
        self.database = database
        self.snapshots = snapshots
        self.fetcher = fetcher
        self.classifier = classifier or SourceTierClassifier.bundled()

    async def add_search_results(
        self, task_id: str, query: str, results: list[SearchResult]
    ) -> list[EvidenceRecord]:
        records = []
        for result in results:
            domain = urlsplit(result.url).hostname or ""
            source_tier, source_role, tier_matched = self.classifier.classify(domain)
            scope_extra = result.raw.get("_scope")
            extra = {
                "source_tier_matched": tier_matched,
                "date_provenance": result.raw.get("date_provenance") or "search_provider",
            }
            if isinstance(scope_extra, dict):
                extra.update(scope_extra)
            record = await self.database.add_evidence(
                EvidenceCreate(
                    task_id=task_id,
                    url=result.url,
                    title=result.title,
                    snippet=result.snippet or result.summary or "无摘要",
                    summary=result.summary,
                    source_name=result.source_name,
                    publisher_entity=self.classifier.canonical_publisher(
                        domain, result.source_name
                    ),
                    source_role=source_role,
                    source_tier=source_tier,
                    published_at=result.published_at.isoformat() if result.published_at else None,
                    retrieval_query=query,
                    provider=result.provider,
                    lang=result.lang,
                    extra=extra,
                )
            )
            records.append(record)
        return records

    async def fetch_one(
        self,
        evidence: EvidenceRecord,
        *,
        scope: InvestigationScope | None = None,
        agent: str = "fact_investigator",
        phase: ScopePhase = "primary",
    ) -> EvidenceRecord:
        if evidence.fetch_status == "fetched":
            return evidence
        try:
            result = await self.fetcher.fetch(evidence.url)
            snapshot_path, digest = self.snapshots.save(evidence.task_id, evidence.pk, result.html)
            published_at, provenance = extract_page_published_at(result.html, result.content_text)
            extra = dict(evidence.extra or {})
            if provenance:
                extra["date_provenance"] = provenance
            if scope is not None:
                scoped_result = SearchResult(
                    url=evidence.url,
                    title=evidence.title,
                    snippet=evidence.snippet or result.content_text[:500],
                    published_at=datetime.fromisoformat(published_at) if published_at else None,
                    source_name=evidence.source_name,
                    provider=evidence.provider or "fetch",
                    lang=evidence.lang or "unknown",
                    raw={"date_provenance": provenance or "unknown"},
                )
                extra.update(
                    scope.classify_result(scoped_result, agent=agent, phase=phase).as_extra()
                )
            await self.database.update_evidence_fetched(
                evidence.task_id,
                evidence.local_id,
                content_text=result.content_text,
                snapshot_path=snapshot_path,
                content_sha256=digest,
                published_at=published_at,
                extra=extra,
            )
        except Exception as exc:
            await self.database.update_evidence_failed(
                evidence.task_id, evidence.local_id, str(exc)
            )
        updated = await self.database.get_evidence(evidence.task_id, evidence.local_id)
        assert updated is not None
        return updated
