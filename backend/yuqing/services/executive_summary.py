"""Attach an independently reviewed narrative to a finalized report."""

from __future__ import annotations

import re

from yuqing.core.event_chronology import action_date_sort_key
from yuqing.core.executive_summary import (
    NUMBER_POLICY,
    SUMMARY_ERROR_LABELS,
    SUMMARY_LIMITS,
    check_paragraphs,
    narrative_errors,
    summary_context,
)

FALLBACK_NOTICE = "综合摘要未通过完整生成与审查，以下为事实提要；请结合正文及局限性阅读。"


def safe_summary_diagnostics(value) -> list[dict]:
    """Publish only controlled codes and numeric positions, never rejected model prose."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value[-12:]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("stage"), str)
            or item.get("stage")
            not in {
                "generation",
                "outline",
                "structure",
                "semantic",
                "scope",
                "assembly",
            }
        ):
            continue
        codes = item.get("codes")
        if not isinstance(codes, list):
            continue
        codes = [c for c in codes if isinstance(c, str) and c in SUMMARY_ERROR_LABELS]
        if not codes:
            continue
        record = {"stage": item["stage"], "codes": list(dict.fromkeys(codes))}
        if (
            type(item.get("attempt")) is int
            and 0 <= item["attempt"] <= SUMMARY_LIMITS["max_draft_attempts"]
        ):
            record["attempt"] = item["attempt"]
        issues = []
        for issue in item.get("issues", []) if isinstance(item.get("issues"), list) else []:
            if (
                not isinstance(issue, dict)
                or not isinstance(issue.get("path"), str)
                or not re.fullmatch(
                    r"paragraphs(?:\[\d+\]\.sentences\[\d+\]|\.(?:event|dispute|findings|limits)(?:\.(?:role_match|focused|gap_disclosed))?)?|review|sentences(?:\.S\d{2})?|coverage(?:\.(?:event|current_state|dispute|findings|boundaries|follow_up))?|coherent|non_redundant|no_material_omission|no_background_rewind",
                    issue["path"],
                )
            ):
                continue
            issue_codes = issue.get("codes")
            if not isinstance(issue_codes, list):
                continue
            accepted = [c for c in issue_codes if isinstance(c, str) and c in codes]
            if accepted:
                issues.append({"path": issue["path"], "codes": accepted})
        if issues:
            record["issues"] = issues[:24]
        result.append(record)
    return result


def fact_brief(block: dict, *, reason: str) -> None:
    block.update(summary_mode="fact_brief", fallback_notice=FALLBACK_NOTICE)
    block.pop("paragraphs", None)
    block.pop("narrative_review", None)
    block.pop("summary_diagnostics", None)
    block["summary_status"] = reason
    block["what"] = sorted(block.get("what", []), key=action_date_sort_key)


def refresh_summary_release(report: dict) -> None:
    """A nonempty array is not proof that a complete summary has been produced."""
    block = next((b for b in report["blocks"] if b.get("type") == "executive_summary"), {})
    quality = report.setdefault("quality", {})
    ready = block.get("summary_mode") == "narrative" and not narrative_errors(block, report)
    quality["executive_summary"] = {
        "status": "complete" if ready else "fact_brief",
        "reason": block.get("summary_status", "unavailable"),
        "diagnostics": safe_summary_diagnostics(block.get("summary_diagnostics")),
    }
    missing = [m for m in quality.get("release_gate_missing", []) if m != "executive_summary"]
    if not ready:
        missing.append("executive_summary")
    quality["release_gate_missing"] = missing
    if quality.get("release_label") != "retrieval_diagnostic":
        quality["release_label"] = "evidence_brief" if missing else "full_report"
    label = quality.get("release_label", "evidence_brief")
    subtitles = {
        "full_report": "完整舆情专报 · 事实、传播与行动依据分层呈现",
        "evidence_brief": "证据简报 · 尚未通过完整舆情专报发布门",
        "retrieval_diagnostic": "检索诊断 · 当前材料不足以生成舆情专报",
    }
    for item in report["blocks"]:
        if item.get("type") == "report_header":
            item["subtitle"] = subtitles[label]
        if item.get("block_id") == "b_00_reading_scope":
            # Preserve the already reviewed topic/window text, replacing only the release note.
            item["release_note"] = {
                "full_report": "已通过完整专报发布门；分析判断与已核验事实分开呈现，仍需关注各条不确定性。",
                "evidence_brief": (
                    "已形成有依据的分析条目，但当前仍是证据简报；尚未通过的完整专报要求见发布缺口。"
                    if quality.get("analysis_items")
                    else "当前为证据简报；尚未通过的完整专报要求见发布缺口。"
                ),
                "retrieval_diagnostic": "当前仅输出检索诊断，没有足够的本事件证据与可核验关键陈述。",
            }[label]


async def attach_executive_summary(
    report: dict, reporter, *, scope_reviewer=None, scope_required=False, cached_only=False
) -> None:
    block = next((b for b in report["blocks"] if b.get("type") == "executive_summary"), None)
    if block is None:
        # Privacy filtering may have removed it. Do not silently regenerate a rejected block.
        refresh_summary_release(report)
        return
    fact_brief(block, reason="unavailable")
    if reporter is not None and hasattr(reporter, "summary_model_notice"):
        notice = reporter.summary_model_notice()
        limits = next((b for b in report["blocks"] if b.get("type") == "limitations"), None)
        if limits is not None:
            limits["items"] = [
                i
                for i in limits["items"]
                if not (i.get("id") == "LS01" and i.get("category") == "摘要审查独立性")
            ]
        if (
            notice
            and limits is not None
            and not any(i.get("text") == notice for i in limits["items"])
        ):
            limits["items"].append({"id": "LS01", "category": "摘要审查独立性", "text": notice})
    context = summary_context(report)
    summarize = getattr(reporter, "cached_summary" if cached_only else "summarize", None)
    if not context["facts"] or summarize is None or (scope_required and scope_reviewer is None):
        refresh_summary_release(report)
        return
    try:
        result = await summarize(context)
        if cached_only and not result:
            refresh_summary_release(report)
            return
        if not isinstance(result, dict):
            result = {}
        candidate = {
            **block,
            "summary_mode": "narrative",
            "paragraphs": result.get("paragraphs"),
            "narrative_review": result.get("narrative_review"),
        }
        diagnostics = safe_summary_diagnostics(result.get("diagnostics"))
        errors = list(
            dict.fromkeys(
                [
                    *check_paragraphs(
                        candidate.get("paragraphs"),
                        context,
                        require_headings=True,
                        number_policy=NUMBER_POLICY,
                    ),
                    *narrative_errors(candidate, report),
                ]
            )
        )
        if errors:
            fact_brief(
                block, reason="unavailable" if result.get("status") == "unavailable" else "rejected"
            )
            if not diagnostics:
                diagnostics = [{"stage": "assembly", "codes": errors}]
        else:
            paragraphs = candidate["paragraphs"]
            if scope_reviewer is not None:
                texts = [s["text"] for p in paragraphs for s in p["sentences"]]
                decisions = await scope_reviewer.review(texts, kind="report_text")
                if len(decisions) != len(texts) or any(
                    not d.allowed or d.text != text
                    for d, text in zip(decisions, texts, strict=True)
                ):
                    # A redacted sentence needs a fresh semantic review; never reuse its approval.
                    fact_brief(block, reason="scope_review_incomplete")
                    block["summary_diagnostics"] = [
                        *diagnostics,
                        {"stage": "scope", "codes": ["scope_review_incomplete"]},
                    ]
                    refresh_summary_release(report)
                    return
            block.update(
                summary_mode="narrative",
                paragraphs=paragraphs,
                narrative_review=candidate["narrative_review"],
                summary_status="accepted",
            )
            block.pop("fallback_notice", None)
            table = next((b for b in report["blocks"] if b.get("type") == "fact_check_table"), None)
            if table is not None:
                table["priority_claim_refs"] = list(
                    dict.fromkeys(
                        r for p in paragraphs for s in p["sentences"] for r in s["claim_refs"]
                    )
                )
        block["summary_diagnostics"] = safe_summary_diagnostics(diagnostics)
    except Exception:
        fact_brief(block, reason="unavailable")
        block["summary_diagnostics"] = [{"stage": "assembly", "codes": ["model_call_unavailable"]}]
    refresh_summary_release(report)
