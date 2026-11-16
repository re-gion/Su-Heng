"""Project dated, cited claims without silently removing stages of the event."""

from __future__ import annotations

import copy
import re
from datetime import date

from yuqing.core.timeline_grouping import group_timeline_events, publication_reading_parts

DATE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_NESTED_OR_CUTOFF_DATE = re.compile(
    r"(?:通报|报道|声明|公告)(?:称|指出|认定)|表示|指出|认定|判定|确认|"
    r"(?:针对|关于|截至|涉及|就|对)\s*$"
)
_UNDATED_NEWS_ATTRIBUTION = re.compile(
    r"^(?:据)?[^，,。；;！？\n]{2,32}?报道(?:称|指出|显示)?[，,]\s*"
)
DISCLOSURE = re.compile(
    r"发帖|发文|发布.{0,12}(?:测试视频|评测|测评|帖文|文章)|首次.{0,8}(?:公开|曝光|披露)"
)
_LETTER_FIELD = re.compile(
    r"(?m)(?:^|\|)\s*(写信人|来信人|写信时间|来信时间|信件内容|来信内容)\s*(?=[:：|]|$)"
)
CHRONOLOGY_NOTE = (
    "按陈述正文明确写出的事件或发布日期排列，不以网页发布日期替代事件日期；"
    "同日先后不明时仅并列展示。保留有引用的节点，不按每日数量或总数截断。"
    "本图仅覆盖已取得并形成陈述的经过，不保证穷尽事件始末；缺少日期或独立陈述的环节仍需补查。"
    "未明确动作时点的回溯陈述保留在事实表，不据其中提及的事件日期推定发布时点。"
)


def explicit_event_date(text: str) -> str | None:
    """Read the leading dated action, never a later date in a retrospective clause.

    The prefix may name an actor (including model numbers) or attribute a record.
    Time-of-day modifiers do not change the day. Invalid and uncertain days stay out.
    """
    attribution = _UNDATED_NEWS_ATTRIBUTION.match(text)
    if attribution and not DATE.search(attribution[0]) and publication_reading_parts(text):
        # The press attribution has no date of its own. Keep the explicitly
        # dated publication in the supported statement as the action.
        text = text[attribution.end() :]
    match = DATE.search(text)
    if not match:
        return None
    prefix = text[: match.start()]
    if len(prefix) > 80 or re.search(r"[，,。；;！？\n]", prefix):
        return None
    if _NESTED_OR_CUTOFF_DATE.search(prefix):
        return None
    if re.match(r"(?:前后|左右|至|到|[-—~～])", text[match.end() :].lstrip()):
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def action_date_sort_key(item: dict) -> tuple[bool, str]:
    stamp = explicit_event_date(item.get("text") or "")
    return (stamp is None, stamp or "")


def dated_action(text: str) -> str | None:
    """A conservative retrieval cue, not a verified event or an instruction."""
    stamp = explicit_event_date(text)
    if not stamp:
        return None
    action = text[DATE.search(text).end() :][:160]
    if re.search(
        r"发文|发帖|发布.{0,12}(?:测试|视频|通报|情况说明)|举报|作出.{0,12}(?:处分|判决)|成立.{0,8}工作组|提起.{0,8}诉讼",
        action,
    ):
        return stamp
    return None


def event_timeline(
    facts: list[dict], *, date_from: str | None = None, date_to: str | None = None
) -> list[dict]:
    """Keep all dated current-event claims with their original text and verification."""
    nodes = []
    seen = set()
    for fact in facts:
        if fact.get("origin_agent") == "history_insight":
            continue
        stamp = explicit_event_date(fact.get("text", ""))
        refs = list(
            dict.fromkeys(
                c["evidence_ref"] for c in fact.get("citations", []) if c.get("evidence_ref")
            )
        )
        if not stamp or not fact.get("claim_ref") or not refs:
            continue
        # Only collapse repeated references, never near-similar events or different verdicts.
        key = fact["claim_ref"]
        if key in seen:
            continue
        seen.add(key)
        nodes.append(
            {
                **copy.deepcopy(fact),
                "date": stamp,
                "window_label": (
                    "重点窗口前的本事件经过"
                    if date_from and stamp < date_from[:10]
                    else "重点窗口后的本事件进展"
                    if date_to and stamp > date_to[:10]
                    else None
                ),
                "claim_refs": [fact["claim_ref"]],
                "evidence_refs": refs,
                "source_name": "时点取自陈述正文",
            }
        )
    return sorted(nodes, key=lambda item: item["date"])


def earlier_event_candidates(facts: list[dict], sources) -> list[dict]:
    """Flag earlier actions or missing same-day disclosures for investigation.

    This detects a known omission, not proof that every stage has been found.
    Dates in biographies, independent cases and unadmitted material are not promoted.
    """
    nodes = event_timeline(facts)
    earliest = nodes[0]["date"] if nodes else "9999-12-31"
    candidates = set()
    for source in sources:
        if source.fetch_status != "fetched" or (source.extra or {}).get("scope_status") not in {
            "main",
            "event_context",
            "foreign_supplement",
        }:
            continue
        text = source.content_text or ""
        # A public mailbox can be hosted on an authority's domain while its
        # dated allegations are still a submission by a member of the public.
        fields = set(_LETTER_FIELD.findall(text[:2000]))
        if fields & {"信件内容", "来信内容"} and fields & {
            "写信人",
            "来信人",
            "写信时间",
            "来信时间",
        }:
            continue
        for passage in re.split(r"[。！？\n]", text):
            stamp = dated_action(passage.strip())
            if not stamp or stamp > earliest:
                continue
            if stamp == earliest and (
                not DISCLOSURE.search(passage)
                or any(
                    DISCLOSURE.search(member["text"])
                    for node in nodes
                    if node["date"] == stamp
                    for member in node.get("related_claims", [node])
                )
            ):
                continue
            candidates.add((stamp, source.local_id))
    return [{"date": stamp, "evidence_ref": ref} for stamp, ref in sorted(candidates)]


def restore_report_chronology(report: dict) -> None:
    """Refresh the reading copy from saved facts; never invent missing early claims."""
    facts = [
        f
        for b in report.get("blocks", [])
        if b.get("type") == "fact_check_table"
        for f in b.get("items", [])
    ]
    task = report.get("task", {})
    nodes = group_timeline_events(
        event_timeline(
            facts, date_from=task.get("time_range_from"), date_to=task.get("time_range_to")
        )
    )
    for block in report.get("blocks", []):
        if block.get("block_id") != "b_02_correction_timeline":
            continue
        if nodes:
            block.pop("chronology_warning", None)
            block.update(
                title="事件经过与各方回应", items=nodes, fallback_text=None, note=CHRONOLOGY_NOTE
            )
            if report.get("quality", {}).get("event_chronology", {}).get("earlier_candidates"):
                warning = "现有材料提及事件起始经过，但尚未形成可展示的独立陈述，事件起点仍待补齐。"
                block["note"] += warning
                block["chronology_warning"] = warning
