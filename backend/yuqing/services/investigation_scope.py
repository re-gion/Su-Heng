from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from yuqing.core.search.base import SearchResult

ScopePhase = Literal["primary", "foreign_supplement"]
ScopeBucket = Literal["main", "background", "history", "foreign_supplement", "pending", "rejected"]

_GENERIC_TOPIC_SUFFIXES = (
    "舆情",
    "舆论",
    "热点",
    "负面新闻",
    "相关新闻",
    "最新消息",
)
_GENERIC_CANDIDATE_TITLES = {
    "媒体武大",
    "专题网站",
    "学校概况",
    "武汉大学",
    "武汉大学新闻网",
    "首页",
    "新闻",
    "faculty",
}
_INSTITUTION_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,24}?(?:大学|学院|公司|集团|医院|学校|银行|政府|委员会|研究院)"
)
_TRUSTED_DATE_PROVENANCE = {
    "page_metadata",
    "page_visible",
    "trusted_structured",
    "user_provided",
}
_DOMESTIC_HOSTS = (
    "people.com.cn",
    "news.cn",
    "xinhuanet.com",
    "cctv.com",
    "cnr.cn",
    "china.com.cn",
    "thepaper.cn",
    "weibo.com",
    "bilibili.com",
    "zhihu.com",
    "baidu.com",
    "qq.com",
    "sina.com.cn",
    "163.com",
    "sohu.com",
    "ifeng.com",
)


def _base_language(value: str | None) -> str:
    return (value or "").split("-", 1)[0].lower()


def detect_text_language(value: str, fallback: str | None = None) -> str:
    """Small deterministic guard; it prevents a provider's requested language becoming truth."""

    if re.search(r"[\u3040-\u30ff]", value):
        return "ja"
    han = len(re.findall(r"[\u4e00-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if han >= 4 and han >= latin * 0.15:
        return "zh"
    if latin >= 4:
        return "en"
    return _base_language(fallback) or "unknown"


def is_topic_discovery_query(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).strip("：:，,。.!！?？")
    return any(
        compact.endswith(suffix) and len(compact[: -len(suffix)]) >= 2
        for suffix in _GENERIC_TOPIC_SUFFIXES
    )


def is_concrete_event_candidate(title: str) -> bool:
    """Reject navigation/category pages while keeping sourced incident headlines."""

    normalized = re.sub(r"\s+", " ", title).strip(" ：:，,。.!！?？|_-")
    folded = normalized.casefold()
    if not normalized or folded in _GENERIC_CANDIDATE_TITLES:
        return False
    if folded.endswith(("首页", "新闻网", "专题网站", "学校概况")):
        return False
    return len(normalized) >= 6


def title_matches_subject(event_query: str, title: str, aliases: Sequence[str] = ()) -> bool:
    anchors = _subject_anchors(event_query, aliases)
    folded = title.casefold()
    return not anchors or any(anchor in folded for anchor in anchors)


def _parse_date(value: str | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _is_domestic_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host.endswith(".cn") or any(
        host == allowed or host.endswith(f".{allowed}") for allowed in _DOMESTIC_HOSTS
    )


def _subject_anchors(event_query: str, aliases: Sequence[str]) -> tuple[str, ...]:
    values = [item.group(0) for item in _INSTITUTION_PATTERN.finditer(event_query)]
    for value in tuple(values):
        if value.endswith("大学") and len(value) >= 4:
            values.append(f"{value[0]}大")
    values.extend(item.strip() for item in aliases if item.strip())
    return tuple(dict.fromkeys(item.casefold() for item in values if len(item.strip()) >= 2))


@dataclass(frozen=True)
class ScopeDecision:
    accepted: bool
    main_eligible: bool
    bucket: ScopeBucket
    reasons: tuple[str, ...] = ()
    detected_language: str = "unknown"
    display_label: str | None = None

    def as_extra(self) -> dict[str, object]:
        return {
            "scope_status": self.bucket,
            "scope_reasons": list(self.reasons),
            "detected_language": self.detected_language,
            "scope_label": self.display_label,
            "main_eligible": self.main_eligible,
        }


@dataclass(frozen=True)
class InvestigationScope:
    event_query: str
    languages: tuple[str, ...]
    source_scope: str = "auto"
    date_from: str | None = None
    date_to: str | None = None
    subject_aliases: tuple[str, ...] = ()

    def classify_result(
        self,
        result: SearchResult,
        *,
        agent: str,
        phase: ScopePhase = "primary",
    ) -> ScopeDecision:
        text = f"{result.title} {result.snippet or ''}"
        detected = detect_text_language(text, result.lang)
        allowed_languages = {_base_language(item) for item in self.languages}
        foreign_supplement = phase == "foreign_supplement"
        reasons: list[str] = []

        anchors = _subject_anchors(self.event_query, self.subject_aliases)
        normalized_text = text.casefold()
        if anchors and not any(anchor in normalized_text for anchor in anchors):
            return ScopeDecision(
                accepted=False,
                main_eligible=False,
                bucket="rejected",
                reasons=("subject_mismatch",),
                detected_language=detected,
            )

        if detected not in allowed_languages and not foreign_supplement:
            reasons.append("language_not_allowed")
        if self.source_scope == "domestic" and not _is_domestic_url(result.url):
            if not foreign_supplement:
                reasons.append("foreign_source_in_domestic_phase")
        if reasons:
            return ScopeDecision(
                accepted=False,
                main_eligible=False,
                bucket="rejected",
                reasons=tuple(reasons),
                detected_language=detected,
            )

        published = _parse_date(result.published_at)
        start = _parse_date(self.date_from)
        end = _parse_date(self.date_to)
        in_range = (
            published is not None
            and (start is None or published >= start)
            and (end is None or published <= end)
        )
        date_provenance = str(result.raw.get("date_provenance") or "search_provider")
        trusted_date = date_provenance in _TRUSTED_DATE_PROVENANCE

        if agent == "history_insight":
            return ScopeDecision(
                accepted=True,
                main_eligible=False,
                bucket="history",
                reasons=() if in_range else ("outside_time_window",),
                detected_language=detected,
                display_label="历史对照",
            )
        has_explicit_window = bool(self.date_from or self.date_to)
        # Provider dates are search hints, not authoritative publication dates. Keep
        # these results fetchable so page metadata or visible text can replace the hint.
        if has_explicit_window and not trusted_date:
            date_reason = "date_unknown" if not published else "date_untrusted"
            return ScopeDecision(
                accepted=True,
                main_eligible=False,
                bucket="pending",
                reasons=(date_reason,),
                detected_language=detected,
                display_label="待确认发布日期",
            )
        if published is not None and not in_range:
            return ScopeDecision(
                accepted=True,
                main_eligible=False,
                bucket="background",
                reasons=("outside_time_window",),
                detected_language=detected,
                display_label="范围外背景",
            )
        if has_explicit_window and not published:
            return ScopeDecision(
                accepted=True,
                main_eligible=False,
                bucket="pending",
                reasons=("date_unknown",),
                detected_language=detected,
                display_label="待确认发布日期",
            )
        if foreign_supplement:
            return ScopeDecision(
                accepted=True,
                main_eligible=True,
                bucket="foreign_supplement",
                detected_language=detected,
                display_label="境外补充",
            )
        return ScopeDecision(
            accepted=True,
            main_eligible=True,
            bucket="main",
            detected_language=detected,
        )


@dataclass(frozen=True)
class ReportReleaseAssessment:
    label: Literal["retrieval_diagnostic", "evidence_brief", "full_report"]
    missing: tuple[str, ...]

    @classmethod
    def evaluate(
        cls,
        *,
        concrete_event: bool,
        main_evidence: int,
        verifiable_key_claims: int,
        in_window_timeline_nodes: int,
        publication_nodes: int,
        propagation_edges: int,
        summary_has_what: bool,
        summary_has_why: bool,
        summary_has_action: bool,
        evidence_bound_recommendations: int,
    ) -> ReportReleaseAssessment:
        retrieval_missing = []
        if not concrete_event:
            retrieval_missing.append("concrete_event")
        if main_evidence < 1:
            retrieval_missing.append("main_evidence")
        if verifiable_key_claims < 1:
            retrieval_missing.append("verifiable_key_claim")
        if retrieval_missing:
            return cls("retrieval_diagnostic", tuple(retrieval_missing))

        missing = []
        if in_window_timeline_nodes < 2:
            missing.append("in_window_timeline")
        if publication_nodes < 2 or propagation_edges < 1:
            missing.append("media_propagation")
        if not (summary_has_what and summary_has_why and summary_has_action):
            missing.append("executive_summary")
        if evidence_bound_recommendations < 1:
            missing.append("evidence_bound_recommendation")
        return cls("full_report" if not missing else "evidence_brief", tuple(missing))
