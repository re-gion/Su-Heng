from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from yuqing.core.fetch.base import FetchProvider
from yuqing.core.fetch.builtin import (
    SOURCE_CREDIT_VERSION,
    extract_page_publication_identity,
    extract_source_credits,
)
from yuqing.core.fetch.chain import FetchChain
from yuqing.core.search.base import SearchResult
from yuqing.storage.db import Database
from yuqing.storage.models import EvidenceCreate, EvidenceRecord, TaskRecord
from yuqing.storage.snapshots import SnapshotStore

from .investigation_scope import InvestigationScope, ScopePhase
from .source_tiers import SourceTierClassifier


class _PublicationMarkup(HTMLParser):
    """Keep article metadata independently of its offset, excluding page furniture."""

    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "wbr",
    }
    _NON_ARTICLE = {
        "nav",
        "navigation",
        "aside",
        "sidebar",
        "footer",
        "related",
        "recommend",
        "breadcrumb",
    }

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        markers = {
            token.lower()
            for key, value in attrs
            if key in {"class", "id"} and value
            for token in re.split(r"[\s_-]+", value)
        }
        blocked = (
            bool(self.stack and self.stack[-1][1])
            or tag in self._NON_ARTICLE
            or bool(markers & self._NON_ARTICLE)
        )
        if tag not in self._VOID:
            self.stack.append((tag, blocked))
        if not blocked:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        blocked = bool(self.stack and self.stack[-1][1])
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if not blocked:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def _article_json_date(markup: str, source_url: str | None) -> str | None:
    for match in re.finditer(
        r'<script\b[^>]*\btype=["\']application/ld\+json["\'][^>]*>(.*?)</script\s*>',
        markup,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]
        else:
            nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kinds = node.get("@type", [])
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if kinds and not set(k for k in kinds if isinstance(k, str)) & {
                "Article",
                "NewsArticle",
                "BlogPosting",
                "Report",
                "ScholarlyArticle",
            }:
                continue
            # Do not recurse into ItemList / related content. Legacy publisher
            # JSON without @type is accepted only as a flat publication record.
            if not kinds and any(isinstance(value, (dict, list)) for value in node.values()):
                continue
            identity = node.get("url") or node.get("mainEntityOfPage")
            if isinstance(identity, dict):
                identity = identity.get("@id")
            if source_url and isinstance(identity, str):
                candidate = urlsplit(urljoin(source_url, identity))
                target = urlsplit(source_url)
                if (candidate.hostname or "").removeprefix("www.") != (
                    target.hostname or ""
                ).removeprefix("www.") or candidate.path.rstrip("/") != target.path.rstrip("/"):
                    continue
            for key in ("datePublished", "pubDate", "publishDate", "publishedAt"):
                if isinstance(node.get(key), str):
                    normalized = _normalize_page_date(node[key])
                    if normalized:
                        return normalized
    return None


def extract_page_published_at(
    raw_html: str, content_text: str, source_url: str | None = None
) -> tuple[str | None, str | None]:
    """Extract only dates visibly carried by the fetched page or its structured metadata."""

    markup = _PublicationMarkup()
    markup.feed(raw_html)
    markup.close()
    decoded = html.unescape("".join(markup.parts))
    structured_patterns = (
        r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished|pubdate|publishdate|publish_time)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished|pubdate|publishdate|publish_time)["\']',
        r'<time\b(?=[^>]*\bitemprop=["\']datePublished["\'])[^>]*\bdatetime=["\']([^"\']+)',
    )
    for pattern in structured_patterns:
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            normalized = _normalize_page_date(match.group(1))
            if normalized:
                return normalized, "page_metadata"
    publisher_search_metadata = re.search(
        r'<span\b[^>]*\bid=["\']pubtime_baidu["\'][^>]*>\s*([^<]{8,40})\s*</span>',
        decoded,
        re.IGNORECASE,
    )
    if publisher_search_metadata:
        normalized = _normalize_page_date(publisher_search_metadata.group(1))
        if normalized:
            return normalized, "page_metadata"
    json_date = _article_json_date(decoded, source_url)
    if json_date:
        return json_date, "page_metadata"
    # An e-paper edition script is page metadata only if it agrees with the
    # edition date embedded in this article URL, not merely a sidebar clock.
    edition = re.search(r'showdate\(\s*["\']([^"\']+)["\']\s*\)', decoded)
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
        decoded,
        re.IGNORECASE,
    )
    if article_header_date:
        normalized = _normalize_page_date(article_header_date.group(1))
        if normalized:
            return normalized, "page_visible"
    article_date_with_source = re.search(
        r"<span>\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
        r"\s*</span>.{0,250}?<span>\s*来源[：:]",
        decoded,
        re.IGNORECASE | re.DOTALL,
    )
    article_time_header = re.search(
        r'<div\b[^>]*class=["\'][^"\']*\btime\b[^"\']*["\'][^>]*>\s*'
        r"<span>\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
        r"\s*</span>",
        decoded,
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
        decoded,
        re.IGNORECASE | re.DOTALL,
    )
    if chinese_header:
        normalized = _normalize_page_date(chinese_header.group(1))
        if normalized:
            return normalized, "page_visible"
    named_article_time = re.search(
        r'<div\b[^>]*class=["\'][^"\']*\bcontent_left_time\b[^"\']*["\'][^>]*>\s*'
        r"(20\d{2}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}(?::\d{2})?)\s+来源\s*[：:]",
        decoded,
        re.IGNORECASE,
    )
    if named_article_time:
        normalized = _normalize_page_date(named_article_time.group(1))
        if normalized:
            return normalized, "page_visible"
    visible = re.search(
        r"^\s*(?:发布时间|发布日期|发布于|时间)\s*[：:]?\s*(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})日?",
        "\n".join(content_text.splitlines()[:6]),
        re.MULTILINE,
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

    async def refresh_task_publication_metadata(self, task: TaskRecord) -> list[EvidenceRecord]:
        """Re-evaluate previously undated captured sources before bounded recovery."""
        scope = InvestigationScope(
            event_query=task.resolved_event_query or task.event_query,
            languages=tuple(task.source_languages),
            source_scope=task.effective_source_scope,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        return [
            await self.refresh_publication_metadata(item, scope=scope)
            for item in await self.database.list_evidence(task.id)
        ]

    async def refresh_publication_metadata(
        self,
        evidence: EvidenceRecord,
        *,
        scope: InvestigationScope | None = None,
        agent: str = "media_propagation",
        phase: ScopePhase = "primary",
    ) -> EvidenceRecord:
        """Revisit an undated captured page without another fetch or claim re-verdict."""
        extra = dict(evidence.extra or {})
        trusted_date = extra.get("date_provenance") in {"page_metadata", "page_visible"}
        if (
            evidence.fetch_status != "fetched"
            or (
                trusted_date
                and "page_publication_identity" in extra
                and extra.get("source_credit_version") == SOURCE_CREDIT_VERSION
            )
            or not evidence.snapshot_path
            or not evidence.content_sha256
        ):
            return evidence

        def captured_metadata():
            path = Path(evidence.snapshot_path).resolve()
            if not path.is_relative_to(self.snapshots.root.resolve()):
                return None
            with gzip.open(path, "rb") as stream:
                payload = stream.read()
            if hashlib.sha256(payload).hexdigest() != evidence.content_sha256:
                return None
            raw_html = payload.decode("utf-8")
            date = (
                (evidence.published_at, extra.get("date_provenance"))
                if trusted_date
                else extract_page_published_at(raw_html, evidence.content_text or "", evidence.url)
            )
            return (
                *date,
                extract_page_publication_identity(raw_html),
                extract_source_credits(raw_html),
            )

        try:
            metadata = await asyncio.to_thread(captured_metadata)
        except (OSError, EOFError, UnicodeError):
            return evidence
        if metadata is None:
            return evidence
        published_at, provenance, identity, source_credits = metadata
        extra["page_publication_identity"] = identity
        extra["page_source_credits"] = source_credits
        extra["source_credit_version"] = SOURCE_CREDIT_VERSION
        if provenance:
            extra["date_provenance"] = provenance
        if scope is not None and published_at and provenance and not trusted_date:
            result = SearchResult(
                url=evidence.url,
                title=evidence.title,
                snippet=evidence.snippet or (evidence.content_text or "")[:500],
                published_at=datetime.fromisoformat(published_at),
                source_name=evidence.source_name,
                provider=evidence.provider or "fetch",
                lang=evidence.lang or "unknown",
                raw={"date_provenance": provenance},
            )
            extra.update(
                scope.classify_result(
                    result,
                    agent=agent,
                    phase=phase,
                    search_query=evidence.retrieval_query,
                    body_text=evidence.content_text,
                ).as_extra()
            )
        # A metadata replay must preserve the original fetch time and snapshot.
        # Sealed investigations remain read-only, as with normal evidence writes.
        await self.database.execute_write(
            """UPDATE evidence SET published_at=COALESCE(?,published_at),extra=?
               WHERE pk=? AND content_sha256=? AND fetch_status='fetched'
               AND EXISTS (SELECT 1 FROM task WHERE id=evidence.task_id
                           AND status NOT IN ('stopping','done'))""",
            (
                published_at,
                json.dumps(extra, ensure_ascii=False),
                evidence.pk,
                evidence.content_sha256,
            ),
        )
        return await self.database.get_evidence(evidence.task_id, evidence.local_id) or evidence

    async def confirm_credited_publication(
        self,
        original: EvidenceRecord,
        credited: EvidenceRecord,
        issuer: str,
        scope: InvestigationScope,
    ) -> EvidenceRecord:
        """Resolve topic relevance for a generic title, never a verification verdict.

        Only the event-keyword miss can be resolved by the same dated publication
        already cited by an in-scope page. Language, region, issuer and page-date
        checks still apply. A propagation edge needs separate independent review.
        """
        extra = original.extra or {}
        if (
            original.fetch_status != "fetched"
            or credited.fetch_status != "fetched"
            or (credited.extra or {}).get("scope_status") not in {"main", "event_context"}
            or set(extra.get("scope_reasons") or []) != {"event_mismatch"}
            or extra.get("date_provenance") not in {"page_metadata", "page_visible"}
            or not original.published_at
            or not credited.published_at
            or original.published_at[:10] != credited.published_at[:10]
            or issuer
            not in f"{original.title} {original.publisher_entity or ''} {original.source_name or ''}"
        ):
            return original
        issuer_scope = InvestigationScope(
            event_query=issuer,
            languages=scope.languages,
            source_scope=scope.source_scope,
            date_from=scope.date_from,
            date_to=scope.date_to,
        )
        decision = issuer_scope.classify_result(
            SearchResult(
                url=original.url,
                title=original.title,
                snippet=original.snippet or "",
                source_name=original.source_name,
                provider=original.provider or "fetch",
                lang=original.lang or "unknown",
                published_at=datetime.fromisoformat(original.published_at),
                raw={"date_provenance": extra["date_provenance"]},
            ),
            agent="media_propagation",
            body_text=original.content_text,
        )
        if not decision.main_eligible:
            return original

        def same_document():
            a = re.sub(r"\s+", "", original.content_text or "")[:12000]
            b = re.sub(r"\s+", "", credited.content_text or "")[:12000]
            match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match()
            return match.size >= max(500, int(min(len(a), len(b)) * 0.85))

        if not await asyncio.to_thread(same_document):
            return original
        await self.database.update_evidence_fetched(
            original.task_id,
            original.local_id,
            content_text=original.content_text,
            snapshot_path=original.snapshot_path,
            content_sha256=original.content_sha256,
            published_at=original.published_at,
            extra={
                **extra,
                **decision.as_extra(),
                "publication_identity": {
                    "credited_evidence_id": credited.local_id,
                    "issuer": issuer,
                    "credited_sha256": credited.content_sha256,
                    "original_sha256": original.content_sha256,
                    "basis": "explicit_credit_same_date_continuous_body",
                },
            },
        )
        return await self.database.get_evidence(original.task_id, original.local_id) or original

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
            return await self.refresh_publication_metadata(
                evidence, scope=scope, agent=agent, phase=phase
            )
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
            extra["page_publication_identity"] = extract_page_publication_identity(result.html)
            extra["source_credit_version"] = SOURCE_CREDIT_VERSION
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
