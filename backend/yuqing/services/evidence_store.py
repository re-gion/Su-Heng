from __future__ import annotations

import asyncio
import hashlib
import html
import re
from datetime import datetime
from urllib.parse import urlsplit

from yuqing.core.fetch.base import FetchProvider
from yuqing.core.fetch.builtin import extract_source_credits
from yuqing.core.fetch.chain import FetchChain
from yuqing.core.search.base import SearchResult
from yuqing.storage.db import Database
from yuqing.storage.models import EvidenceCreate, EvidenceRecord
from yuqing.storage.snapshots import SnapshotStore

from .investigation_scope import InvestigationScope, ScopePhase
from .source_tiers import SourceTierClassifier


def extract_page_published_at(
    raw_html: str, content_text: str, source_url: str | None = None
) -> tuple[str | None, str | None]:
    """Extract only dates visibly carried by the fetched page or its structured metadata."""

    decoded = html.unescape(raw_html)
    header = decoded.split("</head>", 1)[0][:20000]
    structured_patterns = (
        r'["\'](?:datePublished|pubDate|publishDate|publishedAt)["\']\s*:\s*["\']([^"\']+)',
        r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished|pubdate|publishdate|publish_time)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished|pubdate|publishdate|publish_time)["\']',
        r'<time[^>]+datetime=["\']([^"\']+)',
    )
    # Some publishers place article JSON immediately after </head>.
    for region in (header, decoded[:8000]):
        for pattern in structured_patterns:
            match = re.search(pattern, region, re.IGNORECASE)
            if match:
                normalized = _normalize_page_date(match.group(1))
                if normalized:
                    return normalized, "page_metadata"
    publisher_search_metadata = re.search(
        r'<span\b[^>]*\bid=["\']pubtime_baidu["\'][^>]*>\s*([^<]{8,40})\s*</span>',
        decoded[:30000],
        re.IGNORECASE,
    )
    if publisher_search_metadata:
        normalized = _normalize_page_date(publisher_search_metadata.group(1))
        if normalized:
            return normalized, "page_metadata"
    # An e-paper edition script is page metadata only if it agrees with the
    # edition date embedded in this article URL, not merely a sidebar clock.
    edition = re.search(r'showdate\(\s*["\']([^"\']+)["\']\s*\)', decoded[:20000])
    if edition and source_url:
        normalized = _normalize_page_date(edition.group(1))
        if normalized and normalized[:10].replace("-", "") in urlsplit(source_url).path:
            return normalized, "page_metadata"
    # Some article pages show the publication time in their article header
    # without an explicit label. Restrict this to the header's named date node;
    # free-floating dates in sidebars and the body are not publication dates.
    article_header_date = re.search(
        r'<span\b[^>]*class=["\'][^"\']*\btimer\b[^"\']*["\'][^>]*>'
        r"\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)\s*</span>",
        decoded[:16000],
        re.IGNORECASE,
    )
    if article_header_date:
        normalized = _normalize_page_date(article_header_date.group(1))
        if normalized:
            return normalized, "page_visible"
    article_date_with_source = re.search(
        r"<span>\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
        r"\s*</span>.{0,250}?<span>\s*来源[：:]",
        decoded[:12000],
        re.IGNORECASE | re.DOTALL,
    )
    article_time_header = re.search(
        r'<div\b[^>]*class=["\'][^"\']*\btime\b[^"\']*["\'][^>]*>\s*'
        r"<span>\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
        r"\s*</span>",
        decoded[:12000],
        re.IGNORECASE,
    )
    for match in (article_date_with_source, article_time_header):
        if match:
            normalized = _normalize_page_date(match.group(1))
            if normalized:
                return normalized, "page_visible"
    # A Chinese date adjacent to the publisher in the article's heading is
    # publication metadata. Never interpret a sidebar or a body event date here.
    chinese_header = re.search(
        r'</h1>\s*<div\b[^>]*class=["\'][^"\']*\binfo\b[^"\']*["\'][^>]*>'
        r'.{0,900}?<span\b[^>]*class=["\'][^"\']*\bsource\b[^"\']*["\'][^>]*>'
        r"[^<]{1,100}</span>\s*<span\b[^>]*>\s*"
        r"(20\d{2}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}(?::\d{2})?)\s*</span>",
        decoded[:16000],
        re.IGNORECASE | re.DOTALL,
    )
    if chinese_header:
        normalized = _normalize_page_date(chinese_header.group(1))
        if normalized:
            return normalized, "page_visible"
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
    PROVIDER_TEXT_MIN_CHARS = 320

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
            provider_text = str(getattr(result, "content_text", None) or "").strip()
            extra = {
                "source_tier_matched": tier_matched,
                "date_provenance": result.raw.get("date_provenance") or "search_provider",
            }
            if provider_text:
                extra.update(
                    {
                        "content_origin": "provider_fulltext",
                        "provider_text_sha256": hashlib.sha256(
                            provider_text.encode("utf-8")
                        ).hexdigest(),
                        "provider_text_chars": len(provider_text),
                    }
                )
            if result.usage:
                extra["provider_usage"] = result.usage
            if result.provider_metadata:
                extra["provider_metadata"] = result.provider_metadata
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
                    # Provider 返回的长正文能减少第二次抓取，但它不是目标 URL 的
                    # 直接 HTML 快照。保持 discovered 状态并在 extra 中标明来源，
                    # 因而不能用于 verbatim 引述或生成“已存原文快照”标记。
                    content_text=provider_text or None,
                    extra=extra,
                )
            )
            records.append(record)
        return records

    @classmethod
    def has_usable_provider_text(cls, evidence: EvidenceRecord) -> bool:
        extra = evidence.extra or {}
        return (
            extra.get("content_origin") == "provider_fulltext"
            and len((evidence.content_text or "").strip()) >= cls.PROVIDER_TEXT_MIN_CHARS
        )

    @classmethod
    def needs_direct_fetch(cls, evidence: EvidenceRecord) -> bool:
        """Whether a discovered result still needs a target-page snapshot.

        Provider full text is useful for analysis but cannot resolve a pending
        publication-date gate because it is not a response captured from the target
        URL.  Those records must still get a bounded direct-fetch attempt.
        """

        if evidence.fetch_status != "discovered":
            return False
        extra = evidence.extra or {}
        reasons = set(extra.get("scope_reasons") or [])
        pending_date_gate = extra.get("scope_status") == "pending" and bool(
            reasons & {"date_unknown", "date_untrusted"}
        )
        history_date_missing = extra.get("scope_status") == "history" and (
            not evidence.published_at
            or extra.get("date_provenance")
            not in {"page_metadata", "page_visible", "trusted_structured", "user_provided"}
        )
        return (
            pending_date_gate or history_date_missing or not cls.has_usable_provider_text(evidence)
        )

    async def fetch_one(
        self,
        evidence: EvidenceRecord,
        *,
        scope: InvestigationScope | None = None,
        agent: str = "fact_investigator",
        phase: ScopePhase = "primary",
        allow_external_fallback: bool = False,
    ) -> EvidenceRecord:
        if evidence.fetch_status == "fetched":
            return evidence
        try:
            if isinstance(self.fetcher, FetchChain):
                result = await self.fetcher.fetch(
                    evidence.url, allow_fallback=allow_external_fallback
                )
            else:
                result = await self.fetcher.fetch(evidence.url)
            snapshot_path, digest = await asyncio.to_thread(
                self.snapshots.save, evidence.task_id, evidence.pk, result.html
            )
            published_at, provenance = extract_page_published_at(
                result.html, result.content_text, evidence.url
            )
            extra = dict(evidence.extra or {})
            # Credit belongs to this snapshot, including an explicit empty result.
            extra["page_source_credits"] = extract_source_credits(result.html)
            # A successful target-page response supersedes search-provider text.
            extra["content_origin"] = "direct_fetch"
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
                    scope.classify_result(
                        scoped_result,
                        agent=agent,
                        phase=phase,
                        search_query=evidence.retrieval_query,
                        body_text=result.content_text,
                    ).as_extra()
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
