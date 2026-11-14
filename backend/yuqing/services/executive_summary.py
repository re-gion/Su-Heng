"""Attach an independently reviewed narrative to a finalized report."""

from __future__ import annotations

from yuqing.core.executive_summary import narrative_errors, summary_context

FALLBACK_NOTICE = "综合摘要未通过完整生成与审查，以下为事实提要；请结合正文及局限性阅读。"


def fact_brief(block: dict, *, reason: str) -> None:
    block.update(summary_mode="fact_brief", fallback_notice=FALLBACK_NOTICE)
    block.pop("paragraphs", None)
    block.pop("narrative_review", None)
    block["summary_status"] = reason


def refresh_summary_release(report: dict) -> None:
    """A nonempty array is not proof that a complete summary has been produced."""
    block = next((b for b in report["blocks"] if b.get("type") == "executive_summary"), {})
    quality = report.setdefault("quality", {})
    ready = block.get("summary_mode") == "narrative" and not narrative_errors(block, report)
    quality["executive_summary"] = {
        "status": "complete" if ready else "fact_brief",
        "reason": block.get("summary_status", "unavailable"),
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
    report: dict, reporter, *, scope_reviewer=None, scope_required=False
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
    if (
        not context["facts"]
        or reporter is None
        or not hasattr(reporter, "summarize")
        or (scope_required and scope_reviewer is None)
    ):
        refresh_summary_release(report)
        return
    try:
        result = await reporter.summarize(context)
        if not isinstance(result, dict):
            result = {}
        candidate = {
            **block,
            "summary_mode": "narrative",
            "paragraphs": result.get("paragraphs"),
            "narrative_review": result.get("narrative_review"),
        }
        if narrative_errors(candidate, report):
            fact_brief(
                block, reason="unavailable" if result.get("status") == "unavailable" else "rejected"
            )
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
    except Exception:
        fact_brief(block, reason="unavailable")
    refresh_summary_release(report)
