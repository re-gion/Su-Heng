from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from yuqing.core.search.base import SearchResult

ScopePhase = Literal["primary", "foreign_supplement"]
ScopeBucket = Literal[
    "main", "event_context", "background", "history", "foreign_supplement", "pending", "rejected"
]

_GENERIC_TOPIC_SUFFIXES = (
    "舆情",
    "舆论",
    "热点",
    "负面新闻",
    "相关新闻",
    "最新消息",
)
_GENERIC_EVENT_TERMS = (
    "舆情",
    "舆论",
    "热点",
    "事件",
    "争议",
    "通报",
    "回应",
    "官方",
    "调查",
    "复核",
    "情况",
    "结果",
    "最新",
    "相关",
    "后续",
    "发布",
    "处理",
    "问题",
)
_INSTITUTION_PATTERN = re.compile(
    r"[\u4e00-\u9fff]{2,24}?(?:大学|学院|公司|集团|医院|学校|银行|政府|委员会|研究院)"
)
_ENGLISH_INSTITUTION_PATTERN = re.compile(
    r"\b(?:[A-Z][a-z]+\s+){1,4}(?:University|College|Institute|Hospital|School)\b"
)
_ENGLISH_EVENT_ACTION = re.compile(
    r"\b(?:alleg\w*|accus\w*|disciplin\w*|harass\w*|investigat\w*|"
    r"misconduct|review\w*|respond\w*|ruling\w*|scandal|complaint\w*)\b",
    re.IGNORECASE,
)
_HISTORY_ACTION_ZH = (
    "投诉",
    "举报",
    "指控",
    "争议",
    "通报",
    "调查",
    "处分",
    "判决",
    "复核",
    "问责",
    "性骚扰",
)
_HISTORY_INSTITUTION_ZH = ("大学", "高校", "学院", "学校", "中学", "医院", "公司", "集团")
_HISTORY_QUERY_STOPWORDS = {
    "高校",
    "大学",
    "案例",
    "历史案例",
    "舆情",
    "舆情事件",
    "university",
    "student",
    "case",
    "online",
    "public",
    "notice",
    "outcome",
}
_EVENT_ACTION_ZH = (
    "事件",
    "争议",
    "指控",
    "举报",
    "投诉",
    "性骚扰",
    "通报",
    "回应",
    "调查",
    "复核",
    "处理",
    "处分",
    "判决",
    "处罚",
    "道歉",
    "事故",
    "离职",
    "辞职",
    "收购",
)
_ENGLISH_QUERY_CONTEXT_STOPWORDS = {
    "university",
    "college",
    "institute",
    "school",
    "hospital",
    "incident",
    "case",
    "investigation",
    "review",
    "announcement",
    "official",
    "response",
    "public",
    "news",
    "latest",
    "august",
    "september",
    "october",
    "november",
    "december",
    "january",
    "february",
    "march",
    "april",
    "june",
    "july",
    "may",
}
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
    "cyol.com",
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
    "caixin.com",
    "chinanews.com",
    "douyin.com",
    "guancha.cn",
    "jiemian.com",
    "kuaishou.com",
    "toutiao.com",
    "yangtse.com",
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


def title_matches_subject(
    event_query: str,
    title: str,
    aliases: Sequence[str] = (),
    *,
    fallback_to_query: bool = False,
) -> bool:
    anchors = _subject_anchors(event_query, aliases, fallback_to_query=fallback_to_query)
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


def is_domestic_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host.endswith(".cn") or any(
        host == allowed or host.endswith(f".{allowed}") for allowed in _DOMESTIC_HOSTS
    )


def _subject_anchors(
    event_query: str, aliases: Sequence[str], *, fallback_to_query: bool = False
) -> tuple[str, ...]:
    values = [item.group(0) for item in _INSTITUTION_PATTERN.finditer(event_query)]
    values.extend(item.group(0) for item in _ENGLISH_INSTITUTION_PATTERN.finditer(event_query))
    for value in tuple(values):
        if value.endswith("大学") and len(value) >= 4:
            values.append(f"{value[0]}大")
    values.extend(item.strip() for item in aliases if item.strip())
    if not values and fallback_to_query:
        compact = re.sub(r"\s+", "", event_query).strip("：:，,。.!！?？")
        for suffix in _GENERIC_TOPIC_SUFFIXES:
            if compact.endswith(suffix):
                compact = compact[: -len(suffix)]
                break
        if len(compact) >= 2:
            values.append(compact)
    return tuple(dict.fromkeys(item.casefold() for item in values if len(item.strip()) >= 2))


def _event_context_anchors(event_query: str, subject_anchors: Sequence[str]) -> tuple[str, ...]:
    """Extract small, event-specific anchors after removing subject and boilerplate.

    This is deliberately conservative.  It is only a rejection guard for concrete
    event investigations, not a general Chinese segmenter or a relevance score.
    """

    compact = re.sub(r"\s+", "", event_query).casefold()
    for value in sorted(subject_anchors, key=len, reverse=True):
        compact = compact.replace(value, "")
    for value in _GENERIC_EVENT_TERMS:
        compact = compact.replace(value, "")
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}", compact)
    anchors: list[str] = []
    for chunk in chunks:
        if len(chunk) <= 6:
            anchors.append(chunk)
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            anchors.extend(chunk[index : index + 3] for index in range(len(chunk) - 2))
            # Two-character fragments from longer concepts are too ambiguous:
            # 图书 in 图书馆 also matches unrelated 图书资料 pages.
    return tuple(dict.fromkeys(item for item in anchors if len(item) >= 2))


def _history_relevant(text: str, search_query: str | None) -> bool:
    folded = text.casefold()
    if not (
        any(marker in text for marker in _HISTORY_INSTITUTION_ZH)
        or re.search(r"\b(?:university|college|school|campus)\b", folded)
    ):
        return False
    if not (
        any(marker in text for marker in _HISTORY_ACTION_ZH) or _ENGLISH_EVENT_ACTION.search(text)
    ):
        return False
    if not search_query:
        return True
    terms = [
        term.casefold()
        for term in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{4,}", search_query)
        if term.casefold() not in _HISTORY_QUERY_STOPWORDS
    ]
    return sum(term in folded for term in dict.fromkeys(terms)) >= 2


def _english_query_context(search_query: str | None, subjects: Sequence[str]) -> tuple[str, ...]:
    if not search_query:
        return ()
    subject_words = {
        word.casefold() for subject in subjects for word in re.findall(r"[A-Za-z]{4,}", subject)
    }
    return tuple(
        dict.fromkeys(
            word.casefold()
            for word in re.findall(r"[A-Za-z]{4,}", search_query)
            if word.casefold() not in _ENGLISH_QUERY_CONTEXT_STOPWORDS
            and word.casefold() not in subject_words
            and not _ENGLISH_EVENT_ACTION.fullmatch(word)
        )
    )


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
        search_query: str | None = None,
        body_text: str | None = None,
    ) -> ScopeDecision:
        text = f"{result.title} {(result.snippet or '')[:420]}"
        body_lead = (body_text if body_text is not None else result.content_text or "")[:700]
        detected = detect_text_language(text, result.lang)
        allowed_languages = {_base_language(item) for item in self.languages}
        foreign_supplement = phase == "foreign_supplement"
        reasons: list[str] = []

        anchors = _subject_anchors(self.event_query, self.subject_aliases)
        normalized_text = text.casefold()
        if agent == "history_insight":
            history_text = f"{normalized_text} {body_lead.casefold()}"
            event_anchors = _event_context_anchors(self.event_query, anchors)
            if (
                anchors
                and any(anchor in history_text for anchor in anchors)
                and event_anchors
                and any(anchor in history_text for anchor in event_anchors)
            ):
                return ScopeDecision(
                    accepted=False,
                    main_eligible=False,
                    bucket="rejected",
                    reasons=("current_event_not_history",),
                    detected_language=detected,
                )
            if not _history_relevant(f"{text} {body_lead}", search_query):
                return ScopeDecision(
                    accepted=False,
                    main_eligible=False,
                    bucket="rejected",
                    reasons=("history_query_mismatch",),
                    detected_language=detected,
                )
        elif anchors:
            query_language = detect_text_language(self.event_query)
            if detected != query_language:
                trusted_aliases = (
                    *self.subject_aliases,
                    *(
                        match.group(0)
                        for match in _ENGLISH_INSTITUTION_PATTERN.finditer(self.event_query)
                    ),
                )
                subject_anchors = tuple(
                    dict.fromkeys(
                        alias.casefold()
                        for alias in trusted_aliases
                        if detect_text_language(alias) == detected
                    )
                )
            else:
                subject_anchors = anchors
            subject_in_summary = any(anchor in normalized_text for anchor in subject_anchors)
            subject_in_lead = any(anchor in body_lead.casefold() for anchor in subject_anchors)
            if not subject_anchors or not (subject_in_summary or subject_in_lead):
                return ScopeDecision(
                    accepted=False,
                    main_eligible=False,
                    bucket="rejected",
                    reasons=("subject_mismatch",),
                    detected_language=detected,
                )
        event_anchors = _event_context_anchors(self.event_query, anchors)
        query_language = detect_text_language(self.event_query)
        if (
            agent != "history_insight"
            and anchors
            and event_anchors
            and query_language == detected
            and not any(
                anchor in normalized_text or anchor in body_lead.casefold()
                for anchor in event_anchors
            )
        ):
            return ScopeDecision(
                accepted=False,
                main_eligible=False,
                bucket="rejected",
                reasons=("event_mismatch",),
                detected_language=detected,
            )
        if (
            agent != "history_insight"
            and anchors
            and query_language == detected == "zh"
            and any(marker in self.event_query for marker in _EVENT_ACTION_ZH)
            # Later coverage can describe a lawsuit or appeal instead of repeating
            # the original notice's verbs, while still concerning the same incident.
            and not any(marker in text or marker in body_lead for marker in _EVENT_ACTION_ZH)
        ):
            return ScopeDecision(
                accepted=False,
                main_eligible=False,
                bucket="rejected",
                reasons=("event_mismatch",),
                detected_language=detected,
            )
        if detected not in allowed_languages and not foreign_supplement:
            reasons.append("language_not_allowed")
        if self.source_scope == "domestic" and not is_domestic_url(result.url):
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

        if (
            agent != "history_insight"
            and query_language != detected
            and not _ENGLISH_EVENT_ACTION.search(f"{text} {body_lead}")
        ):
            return ScopeDecision(
                accepted=False,
                main_eligible=False,
                bucket="rejected",
                reasons=("event_mismatch",),
                detected_language=detected,
            )
        if agent != "history_insight" and query_language != detected and detected == "en":
            english_subjects = tuple(
                alias.casefold()
                for alias in (
                    *self.subject_aliases,
                    *(
                        match.group(0)
                        for match in _ENGLISH_INSTITUTION_PATTERN.finditer(self.event_query)
                    ),
                )
                if detect_text_language(alias) == "en"
            )
            context = _english_query_context(search_query, english_subjects)
            if context and not any(
                word in f"{normalized_text} {body_lead.casefold()}" for word in context
            ):
                return ScopeDecision(
                    accepted=False,
                    main_eligible=False,
                    bucket="rejected",
                    reasons=("event_mismatch",),
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
        has_explicit_window = bool(self.date_from or self.date_to)

        if agent == "history_insight":
            return ScopeDecision(
                accepted=True,
                main_eligible=False,
                bucket="history",
                reasons=("outside_time_window",) if has_explicit_window and not in_range else (),
                detected_language=detected,
                display_label="独立事件候选（历史对照）",
            )
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
                main_eligible=True,
                bucket="event_context",
                reasons=("outside_time_window",),
                detected_language=detected,
                display_label="本事件时间窗口外补充",
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
        if foreign_supplement and not is_domestic_url(result.url):
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
        event_timeline_nodes: int,
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
        missing = list(retrieval_missing)
        if event_timeline_nodes < 2:
            missing.append("event_timeline")
        if publication_nodes < 2 or propagation_edges < 1:
            missing.append("media_propagation")
        if not (summary_has_what and summary_has_why and summary_has_action):
            missing.append("executive_summary")
        if evidence_bound_recommendations < 1:
            missing.append("evidence_bound_recommendation")
        if retrieval_missing:
            return cls("retrieval_diagnostic", tuple(missing))
        return cls("full_report" if not missing else "evidence_brief", tuple(missing))
