from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


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
    report: dict[str, Any], reader_major: int = 0, reader_minor: int = 1
) -> bool:
    try:
        major, _minor = (int(part) for part in report["schema_version"].split(".", 1))
        required = int(report["min_reader_minor"])
    except (KeyError, TypeError, ValueError):
        return False
    return major == reader_major and reader_minor >= required


def validate_report(report: dict[str, Any]) -> ValidationResult:
    value = copy.deepcopy(report)
    errors: list[str] = []
    warnings: list[str] = []
    if not _version_compatible(value):
        errors.append("R1: IR 版本不兼容，请升级渲染器或执行迁移")

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
    fact_blocks = [block for block in blocks if block.get("type") == "fact_check_table"]
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
        if block.get("type") in {"kpi_grid", "chart"} and not block.get("data_basis"):
            errors.append(f"R7: {block.get('block_id')} 数值块缺 data_basis")
        if block.get("is_editorial") and not block.get("editorial_basis"):
            errors.append(f"R5: {block.get('block_id')} 编辑判断缺依据")
        if block.get("is_editorial") and block.get("section") in {"01", "03"}:
            errors.append(f"R6: {block.get('block_id')} 不得包含编辑判断")
        if block.get("limitation_ref") and block["limitation_ref"] not in limitation_ids:
            warnings.append(f"R12: {block.get('block_id')} 的局限性引用不存在")
            block.pop("limitation_ref", None)
        if block.get("type") == "timeline":
            for node in block.get("nodes", []):
                if not node.get("evidence_refs"):
                    errors.append(f"R8: 时间线节点 {node.get('date')} 缺少证据")
        if block.get("type") == "history_compare":
            for card in block.get("cards", []):
                if not card.get("evidence_refs"):
                    errors.append(f"R8: 历史卡片 {card.get('event_name')} 缺少证据")
        if block.get("type") == "executive_summary":
            for key in ("what", "why", "so_what"):
                for item in block.get(key, []):
                    claim_ref = item.get("claim_ref")
                    if not claim_ref or not _text_supported(
                        claim_texts.get(claim_ref), item.get("text")
                    ):
                        errors.append(f"R14: 执行摘要 {claim_ref or '无引用'} 未被 claim 正文蕴含")

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
