"""Evidence closure for narrative summaries, shared by generation and replay."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from yuqing.core.measurements import measurement_values

SUMMARY_VERSION = "narrative-v1"
COVERAGE_FIELDS = ("event", "current_state", "dispute", "findings", "boundaries", "follow_up")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*(?:%|％)?")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def summary_context(report: dict) -> dict:
    """Use the final, screened report; never source a summary from chapter 07 alone."""
    blocks = report.get("blocks", [])
    facts = {
        item["claim_ref"]: item
        for block in blocks
        if block.get("type") in {"fact_check_table", "historical_facts"}
        for item in block.get("items", [])
    }
    analyses = {}
    reviewed = report.get("quality", {}).get("semantic_review", {}).get("status")
    for block in blocks:
        if block.get("type") in {"analysis", "action_plan"} and reviewed in {"complete", "partial"}:
            for index, item in enumerate(block.get("items", [])):
                analyses[f"{block['block_id']}:{index}"] = {
                    "section": block.get("section"),
                    "target": block["block_id"],
                    **item,
                }
        if block.get("type") == "comment_insight" and block.get("analysis_version") == 5:
            observations = {o["id"]: o for o in block.get("observations", [])}
            for item in block.get("items", []):
                if item.get("review_status") != "accepted":
                    continue
                obs = [
                    observations[r] for r in item.get("observation_refs", []) if r in observations
                ]
                if not obs or any(o.get("review_status") != "accepted" for o in obs):
                    continue
                analyses[f"{block['block_id']}:{item['id']}"] = {
                    "section": "05",
                    "target": f"question-{item['id']}",
                    "kind": "comment_sample",
                    "title": item.get("title"),
                    "summary": item.get("summary")
                    if item.get("summary_review_status") == "accepted"
                    else None,
                    "observations": [o["text"] for o in obs],
                    "sample_count": item.get("sample_count"),
                    "claim_refs": [],
                    "evidence_refs": list(
                        dict.fromkeys(r for o in obs for r in o.get("evidence_refs", []))
                    ),
                    "uncertainty": "仅代表用户确认帖子的已采集样本，不代表总体民意，样本说法不证明事件事实。",
                }
    task = report.get("task", {})
    return {
        "version": SUMMARY_VERSION,
        "task": {
            k: task.get(k)
            for k in (
                "event_query",
                "investigation_scope",
                "time_range_from",
                "time_range_to",
                "created_at",
            )
        },
        "facts": facts,
        "analyses": analyses,
        "timeline": [
            item
            for b in blocks
            if b.get("type") == "timeline"
            for item in b.get("items", b.get("nodes", []))
        ],
        "limitations": {
            item["id"]: item
            for b in blocks
            if b.get("type") == "limitations"
            for item in b.get("items", [])
        },
    }


def _refs(value: Any, allowed: dict) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(r, str) and r in allowed for r in value)
        and len(value) == len(set(value))
    )


def _prose(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(
            _prose(v)
            for k, v in value.items()
            if k
            not in {
                "claim_ref",
                "claim_refs",
                "evidence_ref",
                "evidence_refs",
                "target",
                "section",
                "id",
            }
        )
    if isinstance(value, list):
        return " ".join(_prose(v) for v in value)
    return str(value or "")


def check_paragraphs(paragraphs: Any, context: dict) -> list[str]:
    """Structural, binding, number and repetition guards; NOT semantic entailment."""
    if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 4:
        return ["paragraph_shape"]
    facts, analyses, limits = (context[k] for k in ("facts", "analyses", "limitations"))
    errors, seen, total, count = [], set(), 0, 0
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict) or set(paragraph) != {"sentences"}:
            errors.append("paragraph_shape")
            continue
        sentences = paragraph["sentences"]
        if not isinstance(sentences, list) or not 1 <= len(sentences) <= 10:
            errors.append("sentence_shape")
            continue
        for sentence in sentences:
            count += 1
            if not isinstance(sentence, dict) or set(sentence) != {
                "text",
                "kind",
                "claim_refs",
                "analysis_refs",
                "limitation_refs",
            }:
                errors.append("sentence_shape")
                continue
            text = sentence["text"]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 400
                or re.search(r"[\r\n<>]|\[(?:C|E)\d+\]", text)
            ):
                errors.append("sentence_text")
                continue
            total += len(text)
            if re.search(r"(?:从|基于|据)已审分析|输入上下文|本报告预览", text):
                errors.append("internal_process_leak")
            normalized = re.sub(r"\W", "", text)
            if normalized in seen:
                errors.append("duplicate_sentence")
            seen.add(normalized)
            cr, ar, lr = (sentence[k] for k in ("claim_refs", "analysis_refs", "limitation_refs"))
            if not (_refs(cr, facts) and _refs(ar, analyses) and _refs(lr, limits)):
                errors.append("summary_reference")
                continue
            kind = sentence["kind"]
            if (
                not isinstance(kind, str)
                or kind not in {"fact", "analysis", "limitation"}
                or (
                    kind == "fact"
                    and (not cr or ar or lr)
                    or kind == "analysis"
                    and not ar
                    or kind == "limitation"
                    and (not lr or ar)
                )
            ):
                errors.append("summary_kind")
                continue
            basis = [facts[r].get("text", "") for r in cr] + [limits[r].get("text", "") for r in lr]
            for ref in ar:
                basis.append(
                    {
                        k: analyses[ref].get(k)
                        for k in (
                            "title",
                            "interpretation",
                            "implication",
                            "action",
                            "owner",
                            "trigger",
                            "uncertainty",
                            "summary",
                            "observations",
                            "sample_count",
                        )
                    }
                )
            # Analysis dependencies remain authoritative and must still be in this report.
            for ref in ar:
                for claim in analyses[ref].get("claim_refs", []):
                    if claim not in facts:
                        errors.append("analysis_dependency")
                    else:
                        basis.append(facts[claim].get("text", ""))
            prose = _prose(basis)
            if set(NUMBER.findall(text)) - set(NUMBER.findall(prose)) or measurement_values(
                text
            ) - measurement_values(prose):
                errors.append("unsupported_number")
    if total > 1400 or count > 24:
        errors.append("summary_too_long")
    return list(dict.fromkeys(errors))


def sentence_ids(paragraphs: list[dict]) -> list[str]:
    return [f"S{index:02d}" for index in range(1, sum(len(p["sentences"]) for p in paragraphs) + 1)]


def review_accepted(review: Any, paragraphs: list[dict]) -> bool:
    """Fail closed on absent, partial, duplicate, string-valued or ambiguous decisions."""
    if not isinstance(review, dict):
        return False
    sentences = review.get("sentences")
    ids = sentence_ids(paragraphs)
    if not isinstance(sentences, dict) or set(sentences) != set(ids):
        return False
    if any(decision is not True for decision in sentences.values()):
        return False
    coverage = review.get("coverage")
    return (
        isinstance(coverage, dict)
        and set(coverage) == set(COVERAGE_FIELDS)
        and all(v is True for v in coverage.values())
        and review.get("coherent") is True
        and review.get("non_redundant") is True
        and review.get("no_material_omission") is True
    )


def narrative_errors(block: dict, report: dict) -> list[str]:
    context = summary_context(report)
    paragraphs = block.get("paragraphs")
    errors = check_paragraphs(paragraphs, context)
    if errors:
        return errors
    review = block.get("narrative_review")
    if not isinstance(review, dict) or review.get("version") != SUMMARY_VERSION:
        return ["summary_not_reviewed"]
    if not review_accepted(review, paragraphs):
        errors.append("summary_not_reviewed")
    if review.get("content_digest") != digest(paragraphs) or review.get("basis_digest") != digest(
        context
    ):
        errors.append("summary_review_stale")
    return errors
