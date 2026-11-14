"""Conservative event grouping; authoritative claims remain separate inside each card."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from yuqing.core.claim_semantics import publication_actor

_DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
_ACTION = re.compile(r"发布|公布|发文|发帖|作出|成立|启动|宣布|提交|提起")
_DOCUMENT = re.compile(r"测试视频|检测报告|测试报告|情况说明|通报|声明|评测|测评|判决|裁定")
_NUMBER = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:km/h|mm|N|%|％|台|次|人|份|辆|项))?"
    r"|[零〇一二两三四五六七八九十百千万亿]+(?:台|次|人|份|辆|项|部门)"
)


def _compact(text: str) -> str:
    return re.sub(r"[\W_]", "", text).lower()


def _profile(node: dict) -> dict:
    text = node["text"]
    dated = _DATE.search(text)
    timing = (
        re.match(r"\s*(上午|下午|晚间|夜间|晚|\d{1,2}时)", text[dated.end() :]) if dated else None
    )
    without_date = _DATE.sub("", text).lstrip("，, ")
    actor = publication_actor(text)
    if not actor:
        prefix = re.match(
            r"(.{2,60}?)(?=于|在|就|作出|成立|启动|发文|发帖|宣布|提交|提起)", without_date
        )
        actor = prefix[1] if prefix else ""
    actor = re.sub(r"(?:相关账号|官方账号|账号|方面)$", "", actor)
    action = _ACTION.search(without_date)
    body = without_date[action.end() :] if action else without_date
    document = _DOCUMENT.search(body[:60]) if action and action[0] in {"发布", "公布"} else None
    title = re.match(r"\s*《([^》]+)》", body)
    kind = document[0] if document else action[0] if action else ""
    content = body[document.end() :] if document else body
    step = (
        re.search(r"另一份|另行|第二份|第二次|再次|随后", body[: document.start()])
        if document
        else None
    )
    topic = re.search(r"(?:针对|关于)([^，,。；;《》]{2,30}?)(?:的|问题|事件|事项)", body)
    # Normalize presentation fillers only. Numbers and negation are compared separately.
    normalized = _compact(re.sub(r"相关账号|全部|测试中|制动时|称|[的在中时]", "", content))
    return {
        "actor": _compact(actor),
        "kind": kind,
        "title": _compact(title[1]) if title else "",
        "document": bool(document),
        "timing": timing[1] if timing else "",
        "body": normalized,
        "step": step[0] if step else "",
        "topic": _compact(topic[1]) if topic else "",
        "numbers": _NUMBER.findall(body),
        "negative": re.findall(r"未|没有|否认|不|无|否", body),
        "outcomes": re.findall(r"撤销|撤回|维持|驳回|终止|召回|暂停|恢复|启动|成立", content),
        "scope": re.findall(r"部分|个别|全部|均|仅|所有", content),
        "condition": re.findall(r"静态|动态|纵向|侧向", content),
        "text": _compact(without_date),
    }


def _similar(left: str, right: str) -> bool:
    if left == right:
        return True
    match = SequenceMatcher(None, left, right, autojunk=False)
    shorter = min(len(left), len(right))
    longest = max((part.size for part in match.get_matching_blocks()), default=0)
    return match.ratio() >= 0.86 or (
        shorter >= 24 and longest >= 20 and longest / shorter >= 0.65 and match.ratio() >= 0.7
    )


def _compatible(left: dict, right: dict, a: dict, b: dict) -> bool:
    if left["date"] != right["date"]:
        return False
    if a["title"] and b["title"] and a["title"] != b["title"]:
        return False
    if a["timing"] and b["timing"] and a["timing"] != b["timing"]:
        return False
    if a["step"] != b["step"]:
        return False
    if a["topic"] and b["topic"] and a["topic"] != b["topic"]:
        return False
    if a["numbers"] and b["numbers"] and a["numbers"] != b["numbers"]:
        return False
    for field in ("outcomes", "scope", "condition"):
        if a[field] and b[field] and a[field] != b[field]:
            return False
    if a["negative"] != b["negative"]:
        # A release overview can accompany its substance. Conflicting assertions stay apart.
        outcome = r"发生|存在|发现|断裂|撤销|合格|通过"
        if re.search(outcome, left["text"]) and re.search(outcome, right["text"]):
            return False
    return True


def _same_event(left: dict, right: dict, a: dict, b: dict) -> bool:
    if not left.get("claim_ref") or not right.get("claim_ref"):
        return False
    if not _compatible(left, right, a, b):
        return False
    if a["text"] == b["text"]:
        return True
    if not a["actor"] or a["actor"] != b["actor"] or not a["kind"] or a["kind"] != b["kind"]:
        return False
    if (
        a["negative"] == b["negative"]
        and a["numbers"] == b["numbers"]
        and _similar(a["body"], b["body"])
    ):
        return True
    return bool(
        a["document"]
        and b["document"]
        and set(left["evidence_refs"]) & set(right["evidence_refs"])
        and SequenceMatcher(None, a["body"], b["body"], autojunk=False).find_longest_match().size
        >= 8
    )


def group_timeline_events(nodes: list[dict]) -> list[dict]:
    """Group confident matches without deleting members or aggregating their badges."""
    groups: list[list[tuple[dict, dict]]] = []
    seen = set()
    for node in [member for item in nodes for member in item.get("related_claims", [item])]:
        key = node.get("claim_ref") or (
            node["date"],
            node["text"],
            tuple(node.get("claim_refs", [])),
            tuple(node.get("evidence_refs", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        profile = _profile(node)
        group = next(
            (
                g
                for g in groups
                if all(_compatible(node, old, profile, p) for old, p in g)
                and any(_same_event(node, old, profile, p) for old, p in g)
            ),
            None,
        )
        if group is None:
            groups.append([(node, profile)])
        else:
            group.append((node, profile))
    result = []
    for group in groups:
        members = [node for node, _ in group]
        representative = max(members, key=lambda node: len(_compact(node["text"])))
        item = dict(representative)
        if len(members) > 1:
            item["claim_refs"] = list(
                dict.fromkeys(ref for node in members for ref in node["claim_refs"])
            )
            item["evidence_refs"] = list(
                dict.fromkeys(ref for node in members for ref in node["evidence_refs"])
            )
            item["related_claims"] = members
        result.append(item)
    return result


def event_display_claims(item: dict) -> list[dict]:
    """Use original sentences; fold only overviews or overlapping restatements."""
    members = item.get("related_claims", [item])
    displayed: list[dict] = []
    for member in sorted(members, key=lambda node: len(_compact(node["text"])), reverse=True):
        profile = _profile(member)
        body = _DATE.sub("", member["text"])
        generic = bool(
            re.search(r"(?:回应|涉及).{0,30}(?:相关|有关)?问题[。\s]*$", body)
            and not re.search(r"未|不|宣布|决定|升级|撤销|维持|驳回|\d", profile["body"])
        )
        if displayed and (
            generic
            or any(
                profile["numbers"] == _profile(old)["numbers"]
                and profile["negative"] == _profile(old)["negative"]
                and _similar(profile["body"], _profile(old)["body"])
                for old in displayed
            )
        ):
            continue
        displayed.append(member)
    return displayed
