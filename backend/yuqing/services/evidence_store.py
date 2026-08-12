from __future__ import annotations

from urllib.parse import urlsplit

from yuqing.core.fetch.base import FetchProvider
from yuqing.core.search.base import SearchResult
from yuqing.storage.db import Database
from yuqing.storage.models import EvidenceCreate, EvidenceRecord
from yuqing.storage.snapshots import SnapshotStore

from .source_tiers import SourceTierClassifier


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
            record = await self.database.add_evidence(
                EvidenceCreate(
                    task_id=task_id,
                    url=result.url,
                    title=result.title,
                    snippet=result.snippet or result.summary or "无摘要",
                    summary=result.summary,
                    source_name=result.source_name,
                    publisher_entity=result.source_name or domain,
                    source_role=source_role,
                    source_tier=source_tier,
                    published_at=result.published_at.isoformat() if result.published_at else None,
                    retrieval_query=query,
                    provider=result.provider,
                    extra={"source_tier_matched": tier_matched},
                )
            )
            records.append(record)
        return records

    async def fetch_one(self, evidence: EvidenceRecord) -> EvidenceRecord:
        if evidence.fetch_status == "fetched":
            return evidence
        try:
            result = await self.fetcher.fetch(evidence.url)
            snapshot_path, digest = self.snapshots.save(evidence.task_id, evidence.pk, result.html)
            await self.database.update_evidence_fetched(
                evidence.task_id,
                evidence.local_id,
                content_text=result.content_text,
                snapshot_path=snapshot_path,
                content_sha256=digest,
            )
        except Exception as exc:
            await self.database.update_evidence_failed(
                evidence.task_id, evidence.local_id, str(exc)
            )
        updated = await self.database.get_evidence(evidence.task_id, evidence.local_id)
        assert updated is not None
        return updated
