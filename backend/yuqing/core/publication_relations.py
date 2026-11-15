"""Publication identity and relation-type guards; none of these approve an edge."""

from __future__ import annotations

import re

RELATION_POLICY_VERSION = "document-credit-v1"


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
    original = by_id.get(edge.get("from_evidence_id"), {})
    quote = edge.get("quote")
    if not isinstance(quote, str):
        return []
    names = [original.get(key) for key in ("publisher_entity", "source_name", "publisher")]
    names = [name for name in names if isinstance(name, str) and len(name) >= 2]
    doc_credit = any(
        any(name in match.group() for name in names)
        for match in re.finditer(
            r"(?:^|\n)(?:本文(?:来源|转自|转载自)|信息来源|文章来源|稿件来源|来源|稿源|转载自|转自)\s*[:：/|｜]?\s*[^\n。；]{2,120}",
            quote,
        )
    )
    inline_reference = bool(
        re.search(r"(?:另?据|引述|援引|引用).{1,80}(?:报道|消息|指出|表示)", quote)
    )
    if edge.get("relation") == "repost" and not doc_credit:
        return ["repost_requires_document_credit"]
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
