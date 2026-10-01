from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from yuqing.core.measurements import measurement_values
from yuqing.render.ir_migrations import CURRENT_READER_MINOR


class ReportValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class ValidationResult:
    report: dict[str, Any]
    errors: list[str]
    warnings: list[str]


def _text_supported(claim_text: str | None, summary_text: str | None) -> bool:
    claim = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", claim_text or "")
    summary = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", summary_text or "")
    if not claim or not summary:
        return False
    if summary in claim or claim in summary:
        return True
    summary_pairs = {summary[index : index + 2] for index in range(len(summary) - 1)}
    claim_pairs = {claim[index : index + 2] for index in range(len(claim) - 1)}
    return bool(summary_pairs) and len(summary_pairs & claim_pairs) / len(summary_pairs) >= 0.65


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _version_compatible(
    report: dict[str, Any], reader_major: int = 0, reader_minor: int = CURRENT_READER_MINOR
) -> bool:
    try:
        major, _minor = (int(part) for part in report["schema_version"].split(".", 1))
        required = int(report["min_reader_minor"])
    except (KeyError, TypeError, ValueError):
        return False
    return major == reader_major and reader_minor >= required


def prune_citation_backlinks(report: dict[str, Any]) -> None:
    """Omitted claims cannot remain as clickable backlinks in source cards."""
    bindings = {
        item["claim_ref"]: {c["evidence_ref"] for c in item.get("citations", [])}
        for block in report.get("blocks", [])
        if block.get("type") in {"fact_check_table", "historical_facts"}
        for item in block.get("items", [])
    }
    for block in report.get("blocks", []):
        if block.get("type") == "evidence_appendix":
            for item in block.get("items", []):
                item["citations"] = [
                    c
                    for c in item.get("citations", [])
                    if item["evidence_ref"] in bindings.get(c.get("claim_ref"), set())
                ]


def validate_report(report: dict[str, Any]) -> ValidationResult:
    value = copy.deepcopy(report)
    errors: list[str] = []
    warnings: list[str] = []
    if not _version_compatible(value):
        errors.append("R1: IR 版本不兼容，请升级渲染器或执行迁移")
    quality = value.get("quality", {})
    if not isinstance(quality, dict):
        errors.append("quality 必须是对象")
    elif "chapter_status" in quality:
        states = quality["chapter_status"]
        if not isinstance(states, dict) or any(
            not isinstance(state, dict)
            or state.get("status")
            not in {"complete", "partial", "failed", "missing", "disabled", "unknown"}
            for state in states.values()
        ):
            errors.append("chapter_status 必须包含有效的章节状态")

    blocks = value.get("blocks")
    if not isinstance(blocks, list):
        raise ReportValidationError(errors + ["blocks 必须是数组"])
    block_ids = [block.get("block_id") for block in blocks if isinstance(block, dict)]
    if len(block_ids) != len(set(block_ids)):
        errors.append("block_id 必须唯一")

    limitations = [block for block in blocks if block.get("type") == "limitations"]
    if len(limitations) != 1 or not limitations[0].get("items"):
        errors.append("R11: 局限性声明缺失或为空")
    limitation_ids = {item.get("id") for block in limitations for item in block.get("items", [])}

    appendix = next((block for block in blocks if block.get("type") == "evidence_appendix"), None)
    evidence_ids = {item.get("evidence_ref") for item in (appendix or {}).get("items", [])}
    fact_blocks = [
        block for block in blocks if block.get("type") in {"fact_check_table", "historical_facts"}
    ]
    fact_items = [item for block in fact_blocks for item in block.get("items", [])]
    claim_texts = {item.get("claim_ref"): item.get("text") for item in fact_items}
    candidate_count = value.get("metrics", {}).get("key_claims_candidate", 0)
    claim_ids = {f"C{index:03d}" for index in range(1, candidate_count + 1)}
    bindings = {
        item.get("claim_ref"): {
            citation.get("evidence_ref") for citation in item.get("citations", [])
        }
        for item in fact_items
    }

    for block in blocks:
        if block.get("data_basis") == "illustrative":
            errors.append(f"R4: {block.get('block_id')} 使用 illustrative 数据")
        if block.get("type") in {"kpi_grid", "chart", "metric_cards"} and not block.get(
            "data_basis"
        ):
            errors.append(f"R7: {block.get('block_id')} 数值块缺 data_basis")
        if block.get("is_editorial") and not block.get("editorial_basis"):
            errors.append(f"R5: {block.get('block_id')} 编辑判断缺依据")
        if block.get("is_editorial") and block.get("section") in {"01", "03"}:
            errors.append(f"R6: {block.get('block_id')} 不得包含编辑判断")
        if block.get("limitation_ref") and block["limitation_ref"] not in limitation_ids:
            warnings.append(f"R12: {block.get('block_id')} 的局限性引用不存在")
            block.pop("limitation_ref", None)
        if block.get("type") == "timeline" or block.get("chart_kind") == "timeline":
            for node in block.get("nodes", []) + block.get("items", []):
                if not node.get("evidence_refs"):
                    errors.append(f"R8: 时间线节点 {node.get('date')} 缺少证据")
        if block.get("type") == "action_plan":
            for item in block.get("items", []):
                refs = item.get("claim_refs") or []
                bound = set().union(*(bindings.get(ref, set()) for ref in refs))
                if (
                    not refs
                    or any(ref not in claim_texts for ref in refs)
                    or not item.get("action")
                    or not item.get("owner")
                    or not item.get("trigger")
                    or not item.get("uncertainty")
                    or not item.get("evidence_refs")
                    or not set(item["evidence_refs"]).issubset(bound)
                ):
                    errors.append("R22: 行动清单缺少已审依据或可执行条件")
        if block.get("type") == "comment_insight" and block.get("analysis_version") == 1:
            samples = {s.get("id"): s for s in block.get("samples", [])}
            if len(samples) != len(block.get("samples", [])):
                errors.append("R21: 评论样本编号重复")
            for item in block.get("items", []):
                refs = item.get("comment_refs", [])
                valid = [samples[r] for r in refs if r in samples]
                if (
                    not refs
                    or len(refs) != len(set(refs))
                    or len(valid) != len(refs)
                    or item.get("sample_count") != len(refs)
                    or item.get("review_status") != "accepted"
                ):
                    errors.append("R21: 评论主题计数、引用或审查状态不完整")
                if set(item.get("evidence_refs", [])) != {s.get("evidence_ref") for s in valid}:
                    errors.append("R21: 评论主题来源绑定不匹配")
                from collections import Counter

                if item.get("platform_counts") != dict(Counter(s.get("platform") for s in valid)):
                    errors.append("R21: 评论平台计数与实际样本不一致")
                for quote in item.get("quotes", []):
                    source = samples.get(quote.get("id"), {})
                    if quote.get("id") not in refs or quote.get("text") != source.get("text"):
                        errors.append("R21: 评论引语没有对应主题内原始样本")
        if block.get("type") == "comment_insight" and block.get("analysis_version", 1) >= 4:
            samples = {s.get("id"): s for s in block.get("samples", [])}
            priorities = {"立即回应", "补充说明", "持续观察"}
            item_titles = set()
            for item in block.get("items", []):
                refs = item.get("comment_refs", [])
                valid = [samples[r] for r in refs if r in samples]
                required = (
                    "title",
                    "interpretation",
                    "stance_analysis",
                    "controversy",
                    "risk_assessment",
                    "response_gap",
                    "response_action",
                    "priority_reason",
                    "uncertainty",
                )
                if any(not str(item.get(key) or "").strip() for key in required):
                    errors.append("R23: 评论主题缺少完整的处置分析字段")
                if item.get("priority") not in priorities:
                    errors.append("R23: 评论主题处置优先级无效")
                if (
                    not refs
                    or len(refs) != len(set(refs))
                    or len(valid) != len(refs)
                    or item.get("sample_count") != len(refs)
                    or item.get("review_status") != "accepted"
                ):
                    errors.append("R23: 评论主题计数、引用或审查状态不完整")
                if not isinstance(item.get("stance_counts"), dict) or not isinstance(
                    item.get("platform_counts"), dict
                ):
                    errors.append("R23: 评论主题缺少程序统计的立场或平台分布")
                item_titles.add(item.get("title"))
            for priority in block.get("priority_order", []):
                if (
                    priority.get("title") not in item_titles
                    or priority.get("priority") not in priorities
                ):
                    errors.append("R23: 总体处置排序未绑定已审主题")
        if block.get("type") == "history_compare":
            for card in block.get("cards", []):
                if not card.get("evidence_refs"):
                    errors.append(f"R8: 历史卡片 {card.get('event_name')} 缺少证据")
            for candidate in block.get("excluded_candidates", []):
                if not candidate.get("reason") or not set(
                    candidate.get("evidence_refs", [])
                ).issubset(evidence_ids):
                    errors.append("R8: 历史候选材料排除说明缺少有效依据")
        if block.get("type") == "executive_summary":
            for key in ("what", "why", "so_what"):
                for item in block.get(key, []):
                    if item.get("is_editorial"):
                        refs = item.get("claim_refs") or []
                        bound = set().union(*(bindings.get(ref, set()) for ref in refs))
                        if (
                            not item.get("text")
                            or not item.get("uncertainty")
                            or not refs
                            or any(ref not in claim_texts for ref in refs)
                            or not set(item.get("evidence_refs") or []).issubset(bound)
                        ):
                            errors.append("R19: 执行摘要编辑判断缺少陈述、证据或不确定性")
                        continue
                    claim_ref = item.get("claim_ref")
                    if not claim_ref or not _text_supported(
                        claim_texts.get(claim_ref), item.get("text")
                    ):
                        errors.append(f"R14: 执行摘要 {claim_ref or '无引用'} 未被 claim 正文蕴含")
        if block.get("type") == "analysis":
            if not block.get("is_editorial") or block.get("section") not in {
                "04",
                "05",
                "06",
                "07",
            }:
                errors.append("R19: 综合分析必须标记编辑判断并位于分析章节")
            for item in block.get("items", []):
                refs = item.get("claim_refs") or []
                if not refs or any(ref not in claim_texts for ref in refs):
                    errors.append("R19: 分析缺少可回填的陈述")
                    continue
                observation = "\n".join(claim_texts[ref] for ref in refs)
                if item.get("observation") != observation:
                    errors.append("R19: 分析观察必须由陈述正文回填")
                if not all(
                    item.get(key)
                    for key in ("interpretation", "implication", "uncertainty", "evidence_refs")
                ):
                    errors.append("R19: 分析缺少解释、影响、不确定性或依据")
                if block.get("section") == "07" and not all(
                    item.get(key) for key in ("action", "owner", "trigger")
                ):
                    errors.append("R19: 行动建议缺少执行要素")
        if block.get("type") == "metric_cards":
            if block.get("data_basis") != "quoted_evidence":
                errors.append("R20: 来源披露指标必须使用 quoted_evidence")
            for item in block.get("items", []):
                refs = item.get("evidence_refs") or []
                sources = [
                    e for e in (appendix or {}).get("items", []) if e.get("evidence_ref") in refs
                ]
                quote = item.get("quote") or ""
                if (
                    not quote
                    or not sources
                    or not all(
                        e.get("fetch_status") == "fetched"
                        and quote in e.get("measurement_quotes", [])
                        for e in sources
                    )
                ):
                    errors.append("R20: 来源指标缺少经过原文匹配的引句")
                if (
                    not item.get("claim_refs")
                    or not item.get("scope")
                    or not item.get("verification_note")
                    or str(item.get("value") or "") not in measurement_values(quote)
                    or not item.get("label")
                    or item["label"] not in quote
                    or not any(
                        str(item.get("value") or "")
                        in measurement_values(claim_texts.get(ref) or "")
                        for ref in item.get("claim_refs", [])
                    )
                ):
                    errors.append("R20: 来源指标缺少口径或数字与引句不一致")

    for item in fact_items:
        claim_ref = item.get("claim_ref")
        citations = item.get("citations")
        if not citations:
            errors.append(f"R3: {claim_ref} 没有引用卡片")
        for citation in citations or []:
            quote_type = citation.get("quote_type")
            if quote_type == "verbatim" and (
                citation.get("quote_start") is None or citation.get("quote_end") is None
            ):
                errors.append(f"R9: {claim_ref} 的 verbatim 引用缺少原文偏移")
            if quote_type == "paraphrase" and citation.get("relation") in {
                None,
                "not_mentioned",
            }:
                errors.append(f"R9: {claim_ref} 的 paraphrase 引用未通过核验")
        if item.get("statement_kind") == "fact" and item.get("badge") == "refuted":
            item["badge"] = "unverified"
            warnings.append(f"R10: {claim_ref} 的 fact+refuted 已降级")
            if limitations:
                limitations[0]["items"].append(
                    {
                        "id": f"R10-{claim_ref}",
                        "category": "陈述方向",
                        "text": f"{claim_ref} 的陈述写法与核验方向不一致，已降级为待核验。",
                    }
                )
        if (
            item.get("verification_state", "complete") != "complete"
            and item.get("badge") != "unverified"
        ):
            errors.append(f"R16: {claim_ref} 核验未完成却带确定性徽章")

    for node in _walk(blocks):
        claim_ref = node.get("claim_ref")
        if claim_ref is not None and claim_ref not in claim_ids:
            errors.append(f"R2: claim_ref {claim_ref} 不存在")
        refs: list[str] = []
        for key in ("evidence_ref", "quote_ref"):
            if node.get(key):
                refs.append(node[key])
        refs.extend(node.get("evidence_refs") or [])
        claim_refs = node.get("claim_refs") or []
        for ref in claim_refs:
            if ref not in claim_texts:
                errors.append(f"R2: claim_refs {ref} 未进入报告事实表")
        if claim_refs and refs:
            bound = set().union(*(bindings.get(ref, set()) for ref in claim_refs))
            if not set(refs).issubset(bound):
                errors.append("R17: 多陈述分析引用了未绑定证据")
        for evidence_ref in refs:
            if evidence_ref not in evidence_ids:
                errors.append(f"R2: evidence_ref {evidence_ref} 不存在")
        if (
            claim_ref
            and refs
            and claim_ref in bindings
            and not set(refs).issubset(bindings[claim_ref])
        ):
            errors.append(f"R17: {claim_ref} 引用了未绑定证据")

    metrics = value.get("metrics", {})
    rendered_total = len(fact_items)
    if rendered_total:
        metrics["verified_rate"] = (
            sum(item.get("badge") == "verified" for item in fact_items) / rendered_total
        )
        metrics["disputed_rate"] = (
            sum(item.get("badge") == "disputed" for item in fact_items) / rendered_total
        )
        metrics["refuted_rate"] = (
            sum(item.get("badge") == "refuted" for item in fact_items) / rendered_total
        )
    candidate = metrics.get("key_claims_candidate")
    rendered = metrics.get("key_claims_rendered")
    rejected = metrics.get("key_claims_rejected")
    if (
        not all(isinstance(item, int) for item in (candidate, rendered, rejected))
        or candidate != rendered + rejected
    ):
        errors.append("R18: candidate 必须等于 rendered + rejected")

    if errors:
        raise ReportValidationError(list(dict.fromkeys(errors)))
    return ValidationResult(value, [], warnings)
