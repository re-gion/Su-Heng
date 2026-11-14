"""Evidence closure for narrative summaries, shared by generation and replay."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from yuqing.core.measurements import measurement_values

SUMMARY_VERSION = "narrative-v2"
SUPPORTED_SUMMARY_VERSIONS = {"narrative-v1", SUMMARY_VERSION}
COVERAGE_FIELDS = ("event", "current_state", "dispute", "findings", "boundaries", "follow_up")
SUMMARY_LIMITS = {
    "max_paragraphs": 4,
    "max_sentences_per_paragraph": 10,
    "max_sentence_characters": 400,
    "max_sentences": 24,
    "max_characters": 1400,
    "max_heading_characters": 24,
}
SUMMARY_ERROR_LABELS = {
    "paragraph_shape": "摘要段落结构不符合要求",
    "sentence_shape": "摘要句子字段不完整或包含越权字段",
    "sentence_text": "摘要句子为空、过长或包含不允许的格式",
    "internal_process_leak": "摘要包含内部处理术语",
    "duplicate_sentence": "摘要重复表达同一句内容",
    "summary_reference": "摘要引用未能完整对应报告材料",
    "summary_kind": "句子类型与依据引用不匹配",
    "analysis_dependency": "所引研判依赖的事实不完整",
    "unsupported_number": "摘要数字或单位未能对应所引材料",
    "summary_too_long": "摘要超过段落、句数或字数上限",
    "summary_not_reviewed": "摘要尚无完整有效的内容审查",
    "summary_review_stale": "摘要内容或依据变化，原审查已失效",
    "content_review_rejected": "摘要内容或整体覆盖未通过审查",
    "invalid_review": "摘要审查结果不完整或格式不符合要求",
    "budget_exhausted": "摘要写作或审查额度不足",
    "model_call_unavailable": "摘要写作或审查调用未完成",
    "scope_review_incomplete": "摘要范围与隐私审查未完成或需要重新核验",
    "sentence_not_supported": "摘要中有句子未获所引材料支持",
    "coverage_gap": "摘要遗漏关键内容或后续关注",
    "summary_incoherent": "摘要标题或段落衔接未通过审查",
    "summary_redundant": "摘要内容重复",
    "summary_material_omission": "摘要遗漏会影响整体判断的材料或边界",
    "heading_text": "摘要段落小标题缺失、过长或格式无效",
    "duplicate_heading": "摘要段落小标题重复",
}
NUMBER = re.compile(r"\d+(?:[.,]\d+)*(?:%|％)?")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def summary_context(report: dict, *, version: str = SUMMARY_VERSION) -> dict:
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
    value = {
        "version": version,
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
    if version == "narrative-v1":
        return value
    # Keep all factual text and evidence relations, without repeating presentation
    # fields, offsets and database observations that already exist in facts.
    value["timeline"] = [
        item
        for b in blocks
        if b.get("type") == "timeline" or b.get("chart_kind") == "timeline"
        for item in b.get("items", b.get("nodes", []))
    ]
    value["basis_digest"] = digest(value)
    fact_fields = {
        "claim_ref",
        "origin_agent",
        "statement_kind",
        "text",
        "rumor_text",
        "correction_text",
        "badge",
        "verdict",
        "verification_state",
        "verification_basis",
        "independent_sources",
        "max_source_tier",
        "evidence_grade",
    }
    citation_fields = {
        "evidence_ref",
        "quote",
        "relation",
        "source_role",
        "fetch_status",
        "source_name",
    }
    value["facts"] = {
        ref: {
            **{k: v for k, v in item.items() if k in fact_fields},
            "citations": [
                {k: v for k, v in c.items() if k in citation_fields}
                for c in item.get("citations", [])
            ],
        }
        for ref, item in facts.items()
    }
    value["analyses"] = {
        ref: {k: v for k, v in item.items() if k != "observation"} for ref, item in analyses.items()
    }
    value["limitations"] = {
        ref: item
        for ref, item in value["limitations"].items()
        if item.get("category") != "协作编排"
    }
    return value


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


def check_paragraphs(
    paragraphs: Any, context: dict, *, require_headings: bool = False
) -> list[str]:
    """Structural, binding, number and repetition guards; NOT semantic entailment."""
    if (
        not isinstance(paragraphs, list)
        or not 1 <= len(paragraphs) <= SUMMARY_LIMITS["max_paragraphs"]
    ):
        return ["paragraph_shape"]
    facts, analyses, limits = (context[k] for k in ("facts", "analyses", "limitations"))
    errors, seen, total, count = [], set(), 0, 0
    headings = set()
    for paragraph in paragraphs:
        if (
            not isinstance(paragraph, dict)
            or "sentences" not in paragraph
            or set(paragraph) - {"heading", "sentences"}
        ):
            errors.append("paragraph_shape")
            continue
        heading = paragraph.get("heading")
        if "heading" in paragraph or require_headings:
            if (
                not isinstance(heading, str)
                or not heading.strip()
                or len(heading) > SUMMARY_LIMITS["max_heading_characters"]
                or re.search(r"[\r\n<>]|\[(?:C|E)\d+\]", heading)
            ):
                errors.append("heading_text")
            else:
                total += len(heading)
                normalized_heading = re.sub(r"\W", "", heading)
                if normalized_heading in headings:
                    errors.append("duplicate_heading")
                headings.add(normalized_heading)
        heading_basis = []
        sentences = paragraph["sentences"]
        if (
            not isinstance(sentences, list)
            or not 1 <= len(sentences) <= SUMMARY_LIMITS["max_sentences_per_paragraph"]
        ):
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
                or len(text) > SUMMARY_LIMITS["max_sentence_characters"]
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
                    and (not cr or ar)
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
            heading_basis.append(prose)
            if set(NUMBER.findall(text)) - set(NUMBER.findall(prose)) or measurement_values(
                text
            ) - measurement_values(prose):
                errors.append("unsupported_number")
        if isinstance(heading, str) and (
            set(NUMBER.findall(heading)) - set(NUMBER.findall(" ".join(heading_basis)))
            or measurement_values(heading) - measurement_values(" ".join(heading_basis))
        ):
            errors.append("unsupported_number")
    if total > SUMMARY_LIMITS["max_characters"] or count > SUMMARY_LIMITS["max_sentences"]:
        errors.append("summary_too_long")
    return list(dict.fromkeys(errors))


def paragraph_issues(
    paragraphs: Any, context: dict, *, require_headings: bool = False
) -> list[dict]:
    """Locate structural failures without copying text or arbitrary model identifiers."""
    errors = check_paragraphs(paragraphs, context, require_headings=require_headings)
    if not errors:
        return []
    issues = []
    located = set()
    total = count = 0
    if isinstance(paragraphs, list):
        for pi, paragraph in enumerate(paragraphs):
            if not isinstance(paragraph, dict) or not isinstance(paragraph.get("sentences"), list):
                continue
            for si, sentence in enumerate(paragraph["sentences"]):
                count += 1
                if isinstance(sentence, dict) and isinstance(sentence.get("text"), str):
                    total += len(sentence["text"])
                codes = check_paragraphs([{"sentences": [sentence]}], context)
                if codes:
                    issues.append({"path": f"paragraphs[{pi}].sentences[{si}]", "codes": codes})
                    located.update(codes)
    remaining = [code for code in errors if code not in located]
    if remaining:
        issues.append(
            {"path": "paragraphs", "codes": remaining, "characters": total, "sentences": count}
        )
    return issues


def sentence_ids(paragraphs: list[dict]) -> list[str]:
    return [f"S{index:02d}" for index in range(1, sum(len(p["sentences"]) for p in paragraphs) + 1)]


def review_accepted(review: Any, paragraphs: list[dict]) -> bool:
    """Fail closed on absent, partial, duplicate, string-valued or ambiguous decisions."""
    return not review_issues(review, paragraphs)


def review_issues(review: Any, paragraphs: list[dict]) -> list[dict]:
    """Keep exact review locations, without publishing the reviewer's free text."""
    if not isinstance(review, dict):
        return [{"path": "review", "codes": ["invalid_review"]}]
    issues = []
    sentences = review.get("sentences")
    ids = sentence_ids(paragraphs)
    if not isinstance(sentences, dict) or set(sentences) != set(ids):
        issues.append({"path": "sentences", "codes": ["invalid_review"]})
    else:
        for ref in ids:
            if sentences[ref] is not True:
                code = "sentence_not_supported" if sentences[ref] is False else "invalid_review"
                issues.append({"path": f"sentences.{ref}", "codes": [code]})
    coverage = review.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != set(COVERAGE_FIELDS):
        issues.append({"path": "coverage", "codes": ["invalid_review"]})
    else:
        for field in COVERAGE_FIELDS:
            if coverage[field] is not True:
                code = "coverage_gap" if coverage[field] is False else "invalid_review"
                issues.append({"path": f"coverage.{field}", "codes": [code]})
    for field, code in (
        ("coherent", "summary_incoherent"),
        ("non_redundant", "summary_redundant"),
        ("no_material_omission", "summary_material_omission"),
    ):
        if review.get(field) is not True:
            issues.append(
                {"path": field, "codes": [code if review.get(field) is False else "invalid_review"]}
            )
    return issues


def narrative_errors(block: dict, report: dict) -> list[str]:
    review = block.get("narrative_review")
    if not isinstance(review, dict) or review.get("version") not in SUPPORTED_SUMMARY_VERSIONS:
        return ["summary_not_reviewed"]
    context = summary_context(report, version=review["version"])
    paragraphs = block.get("paragraphs")
    errors = check_paragraphs(paragraphs, context)
    if errors:
        return errors
    if not review_accepted(review, paragraphs):
        errors.append("summary_not_reviewed")
    if review.get("content_digest") != digest(paragraphs) or review.get("basis_digest") != digest(
        context
    ):
        errors.append("summary_review_stale")
    return errors
