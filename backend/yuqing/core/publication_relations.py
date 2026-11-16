"""Publication identity and relation-type guards; none of these approve an edge."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

RELATION_POLICY_VERSION = "document-credit-v1"
DOCUMENT_CREDIT = re.compile(
    r"(?:信息来源|文章来源|稿件来源|稿源|来源|转载自|转自)\s*[:：/|｜]\s*[^\n。；;]{2,100}"
)
INLINE_CITATION = re.compile(r"(?:另?据|援引|引述|引用)[^\n，,。；：:]{2,45}(?:消息|报道)[，,：:]")
_COMBINED_CREDIT = re.compile(r"综合|整理自|[、，,]|等(?:媒体|报道)?$")


def document_credit_publisher(quote: str) -> str | None:
    """Read one explicit document credit, excluding composite bylines."""
    match = DOCUMENT_CREDIT.match(quote.strip())
    if not match:
        return None
    credit = re.split(r"[:：/|｜]", match.group(), maxsplit=1)[1].strip()
    credit = re.split(r"[|｜（(]|\s*(?=20\d{2}[年-])", credit, maxsplit=1)[0].strip()
    if not credit or _COMBINED_CREDIT.search(credit):
        return None
    return credit


def publication_endpoint(source: dict) -> dict:
    publisher = (
        source.get("publisher") or source.get("publisher_entity") or source.get("source_name")
    )
    host = urlsplit(source.get("url") or "").hostname
    return {
        "publisher": publisher,
        "carrier": source.get("platform") or publisher or host,
        "host": re.sub(r"^(?:www|wap|m)\.", "", host or ""),
        "published_at": source.get("published_at"),
    }


def _target_precedes_source(original: dict, target: dict) -> bool:
    try:
        a, b = (
            datetime.fromisoformat(item.get("published_at") or "") for item in (original, target)
        )
    except ValueError:
        return False
    if (
        (a.tzinfo is None) == (b.tzinfo is None)
        and a.time() != datetime.min.time()
        and b.time() != datetime.min.time()
    ):
        return b < a
    return b.date() < a.date()


def relation_direction_reversed(edge: dict, by_id: dict) -> bool:
    source, target = edge.get("from_evidence_id"), edge.get("to_evidence_id")
    return (
        isinstance(source, str)
        and isinstance(target, str)
        and source in by_id
        and target in by_id
        and _target_precedes_source(by_id[source], by_id[target])
    )


def inline_citation_passage(text: str, match: re.Match) -> str:
    passage = re.match(r"[^\n。；]{1,200}[。；]?", text[match.end() :])
    return passage.group() if passage else ""


def extract_publication_identity(text: str) -> dict:
    """A hosting disclaimer separates its carrier from an explicitly named newsroom."""
    declaration = re.search(
        r"(?:^|[。；\n])\s*([^。；\n]{2,30}?)仅提供(?:信息)?(?:发布|存储空间)?(?:平台|服务)",
        text,
    )
    if declaration is None:
        return {}
    byline = re.search(
        r"(?:^|\n)(?:文|文字|撰文|采写|作者|报道|漫画)\s*[丨|｜：:]\s*"
        r"([^\s|丨。；（）()]{2,30}?)(?:首席)?(?:记者|通讯员|插画师)",
        text,
    )
    return {
        "platform_only": True,
        "platform": re.sub(
            r"^(?:特别声明|免责声明|声明)\s*[:：]\s*", "", declaration.group(1)
        ).strip(),
        "publisher": re.sub(r"(?:全媒体|融媒体|新媒体)$", "", byline.group(1)).strip()
        if byline
        else None,
        "basis": "platform_disclaimer_and_newsroom_byline" if byline else "platform_disclaimer",
    }


def publication_identity(source, *, fallback_publisher=None) -> dict:
    def field(key, default=None):
        return (
            source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)
        )

    extra = field("extra") or {}
    identity = extra.get("page_publication_identity") or extract_publication_identity(
        field("content_text") or ""
    )
    default = (
        fallback_publisher
        or field("publisher_entity")
        or field("source_name")
        or field("source_domain")
    )
    if identity.get("platform_only"):
        carrier = identity.get("platform")
        if carrier in {"本平台", "平台", "本网站", "网站"}:
            carrier = field("source_name") or field("source_domain")
        return {
            "publisher": identity.get("publisher") or "作者身份待核",
            "platform": carrier,
            "identity_status": "platform_author" if identity.get("publisher") else "author_unknown",
        }
    carrier = field("publisher_entity") or field("source_name") or field("source_domain")
    for credit in extra.get("page_source_credits", []):
        match = re.match(
            r"(?:信息来源|文章来源|稿件来源|稿源|来源)\s*[:：/|｜]\s*([^\s，,。；]{2,80})", credit
        )
        if (
            match
            and match.group(1) != carrier
            and match.group(1) not in {"网络", "互联网", "本站", "本网站", "未知", "综合"}
        ):
            return {
                "publisher": match.group(1),
                "platform": carrier,
                "identity_status": "credited_source",
            }
    return {"publisher": default}


def relation_type_codes(edge: dict, by_id: dict) -> list[str]:
    """Inline attribution proves a citation, never a whole-document repost by itself."""
    relation = edge.get("relation")
    if relation_direction_reversed(edge, by_id):
        return [
            "repost_precedes_original" if relation == "repost" else "relation_precedes_original"
        ]
    if edge.get("support_evidence_id") != edge.get("to_evidence_id"):
        return ["relation_requires_target_quote"]
    original = by_id.get(edge.get("from_evidence_id"), {})
    target = by_id.get(edge.get("to_evidence_id"), {})
    quote = edge.get("quote")
    if not isinstance(quote, str):
        return []
    history_citations = target.get("history_citations")
    if original.get("history_citations") or (
        history_citations
        and (
            edge.get("relation") != "follow_up"
            or edge.get("support_evidence_id") != edge.get("to_evidence_id")
            or quote not in history_citations.get(edge.get("from_evidence_id"), ())
        )
    ):
        return ["outside_confirmed_publication_citation"]
    names = [original.get(key) for key in ("publisher_entity", "source_name", "publisher")]
    original_body = original.get("article_body_sha256")
    if original_body and original_body == target.get("article_body_sha256"):
        names.append(original.get("platform"))
    names = [name for name in names if isinstance(name, str) and len(name) >= 2]
    doc_credit = any(
        any(name in match.group() for name in names)
        for match in re.finditer(
            r"(?:^|\n)(?:本文(?:来源|转自|转载自)|信息来源|文章来源|稿件来源|来源|稿源|转载自|转自)\s*[:：/|｜]?\s*[^\n。；]{2,120}",
            quote,
        )
    )
    if edge.get("relation") == "repost":
        a, b = publication_endpoint(original), publication_endpoint(target)
        if (a["carrier"] and a["carrier"] == b["carrier"]) or (
            a["host"] and a["host"] == b["host"]
        ):
            return ["same_publication_carrier"]
    inline_reference = bool(
        re.search(r"(?:另?据|引述|援引|引用).{1,80}(?:报道|消息|指出|表示)", quote)
    )
    if edge.get("relation") == "repost" and not doc_credit:
        return ["repost_requires_document_credit"]
    credit = DOCUMENT_CREDIT.match(quote.strip())
    if edge.get("relation") == "repost" and credit:
        publisher = document_credit_publisher(credit.group())
        if publisher is None:
            return ["repost_requires_single_document_credit"]
        if original.get("identity_status") == "credited_source" and publisher == original.get(
            "publisher"
        ):
            return ["credit_points_to_upstream_publisher"]
    if edge.get("relation") == "follow_up" and doc_credit and not inline_reference:
        return ["source_credit_is_not_follow_up"]
    return []


def prune_relation_conflicts(edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Conflicting types for the same pair and passage require a fresh adjudication."""
    groups = {}
    for edge in edges:
        quote = re.sub(r"\s+", "", str(edge.get("quote") or ""))
        key = (edge.get("from_evidence_id"), edge.get("to_evidence_id"), quote)
        groups.setdefault(key, []).append(edge)
    rejected = []
    for key, group in groups.items():
        for other_key, other in groups.items():
            if key[:2] != other_key[:2] or not key[2] or not other_key[2]:
                continue
            if key[2] not in other_key[2] and other_key[2] not in key[2]:
                continue
            if len({edge.get("relation") for edge in [*group, *other]}) > 1:
                rejected.extend(group)
                break
    bad = {id(edge) for edge in rejected}
    unique = {}
    for edge in edges:
        if id(edge) not in bad:
            key = (edge.get("from_evidence_id"), edge.get("to_evidence_id"), edge.get("relation"))
            unique.setdefault(key, edge)
    return list(unique.values()), rejected
