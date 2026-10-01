from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Protocol

from yuqing.core.history_cards import unique_history_cards
from yuqing.render.html import render_html
from yuqing.render.validator import prune_citation_backlinks, validate_report
from yuqing.services.forum import ForumMessage
from yuqing.services.historical_data import HistoricalDataService, HotSnapshotPoint
from yuqing.services.history_comparison import independent_case_evidence
from yuqing.services.institution_scope import PROTECTED_SCOPES, InstitutionScopeReviewer
from yuqing.services.investigation_scope import ReportReleaseAssessment
from yuqing.services.report_analysis import (
    assemble_analysis,
    deduplicate_limitations,
    distinct_facts,
    event_timeline,
    report_context,
    select_priority_timeline_nodes,
)
from yuqing.services.report_builder import BriefReportBuilder, EntailmentVerifier
from yuqing.services.task_diagnostics import task_timing
from yuqing.services.translation import Translator
from yuqing.storage.db import Database


class ReportSectionAgent(Protocol):
    async def enrich(self, context: dict[str, Any]) -> dict[str, Any]: ...


class FullReportBuilder:
    """从数据库权威字段组装完整 IR；LLM 只能补充非权威叙述块。"""

    def __init__(
        self,
        database: Database,
        reports_dir: Path,
        entailment_verifier: EntailmentVerifier | None = None,
        reporter: ReportSectionAgent | None = None,
        historical_data: HistoricalDataService | None = None,
        translator: Translator | None = None,
        scope_reviewer: InstitutionScopeReviewer | None = None,
    ):
        self.database = database
        self.reports_dir = Path(reports_dir)
        self.brief = BriefReportBuilder(
            database, reports_dir, entailment_verifier, scope_reviewer=scope_reviewer
        )
        self.reporter = reporter
        self.historical_data = historical_data or HistoricalDataService(database)
        self.translator = translator
        self.scope_reviewer = scope_reviewer

    @staticmethod
    def redact_review_diagnostics(report: dict[str, Any]) -> None:
        # Model review prose may quote unreviewed source identities. Keep safe structure.
        semantic = report.get("quality", {}).get("semantic_review", {})
        for chapter in semantic.get("chapters", {}).values():
            for reason in chapter.get("reasons", []):
                if isinstance(reason, dict) and "reason" in reason:
                    reason["reason"] = "未通过证据与语义审查；详细记录保留在本地检查点。"

    @staticmethod
    def _hide_scoped_source_text(report: dict[str, Any]) -> None:
        """Keep source links while removing unreviewed source text from every export."""

        FullReportBuilder.redact_review_diagnostics(report)
        report["task"]["user_note"] = None
        report["task"].pop("original_query", None)
        report["task"].pop("resolved_event_query", None)

        def hide_quotes(value):
            if isinstance(value, dict):
                if value.get("type") == "metric_cards":
                    return
                for key in ("quote", "support_quote", "cited_sentence", "support_excerpt"):
                    if key in value:
                        value[key] = None
                for v in value.values():
                    hide_quotes(v)
            elif isinstance(value, list):
                for v in value:
                    hide_quotes(v)

        hide_quotes(report["blocks"])
        for block in report.get("blocks", []):
            if block.get("type") == "evidence_appendix":
                for item in block.get("items", []):
                    item["title"] = f"公开来源 {item['evidence_ref']}"
                    item["source_name"] = "公开来源"
                    item["publisher_entity"] = None
                    item["snapshot_pk"] = None
                    item["original_excerpt"] = None
                    item["machine_translation_zh"] = None
                    item["measurement_quotes"] = []
                    for citation in item.get("citations", []):
                        citation["quote"] = None
                        citation["quote_redacted"] = True
            if block.get("type") in {"fact_check_table", "historical_facts"}:
                for item in block.get("items", []):
                    item["rumor_text"] = None
                    item["correction_text"] = None
                    item["verify_reason"] = None
                    for citation in item.get("citations", []):
                        citation.update(
                            quote=None, quote_type="redacted", quote_start=None, quote_end=None
                        )

    async def _retain_reviewed_blocks(self, report, task, *, normalize_source_labels=True):
        """A failed optional review cannot replace already reviewed authoritative facts."""
        texts = set()
        ignored_keys = {
            "url",
            "origin_url",
            "snapshot_path",
            "content_sha256",
            "diagnostics",
            "samples",
        }

        def collect(value, key=""):
            if key in ignored_keys:
                return set()
            found = set()
            if isinstance(value, dict):
                for k, v in value.items():
                    found.update(collect(v, k))
            elif isinstance(value, list):
                for v in value:
                    found.update(collect(v, key))
            elif isinstance(value, str) and any("\u4e00" <= c <= "\u9fff" for c in value):
                found.add(value)
            return found

        # Facts and citation cards were screened before assembly; do not re-decide them.
        protected_types = {
            "fact_check_table",
            "historical_facts",
            "evidence_appendix",
            "report_header",
        }
        units = []
        for block in report["blocks"]:
            if block["type"] in protected_types:
                continue
            if isinstance(block.get("items"), list):
                for item in block["items"]:
                    part = collect(item)
                    units.append((block, item, part))
                    texts.update(part)
                metadata = collect(
                    {k: v for k, v in block.items() if k not in {"items", "samples"}}
                )
                units.append((block, None, metadata))
                texts.update(metadata)
            elif block["type"] == "executive_summary":
                for field in ("what", "why", "so_what"):
                    for item in block.get(field, []):
                        if field == "what" and item.get("claim_ref"):
                            continue
                        part = collect(item)
                        units.append((block[field], item, part))
                        texts.update(part)
            else:
                part = collect(block)
                units.append((block, None, part))
                texts.update(part)
        core_texts = set().union(
            *(
                part
                for owner, item, part in units
                if isinstance(owner, list)
                or isinstance(owner, dict)
                and owner.get("section") in {"01", "02", "04", "07"}
            )
        )
        ordered = sorted(texts, key=lambda text: (text not in core_texts, text))
        if self.scope_reviewer:
            self.scope_reviewer.bind(self.database, task.id, task.investigation_scope)
            decisions = await self.scope_reviewer.review(ordered, kind="report_text")
            if normalize_source_labels:
                approved_labels = {
                    text: decision.text
                    for text, decision in zip(ordered, decisions, strict=True)
                    if decision.allowed and decision.text != text
                }
                changes = self._redact_source_labels(report, approved_labels)
                if changes:
                    # Only source display labels change. Re-collect and review the new
                    # representations; facts, semantic analysis and relation IDs stay intact.
                    await self._retain_reviewed_blocks(report, task, normalize_source_labels=False)
                    report["quality"]["scope_review"]["redacted_source_labels"] = changes
                    return
            safe = {
                text
                for text, d in zip(ordered, decisions, strict=True)
                if d.allowed and d.text == text
            }
            rejected = sum(d.status == "rejected" for d in decisions)
            incomplete = sum(
                d.status == "incomplete" or d.allowed and d.text != text
                for text, d in zip(ordered, decisions, strict=True)
            )
        else:
            safe, rejected, incomplete = set(), 0, len(ordered)
        removed_blocks = set()
        for owner, item, part in units:
            if part <= safe:
                continue
            if isinstance(owner, list):
                if item in owner:
                    owner.remove(item)
            elif item is not None:
                if item in owner["items"]:
                    owner["items"].remove(item)
            else:
                removed_blocks.add(owner["block_id"])
        report["blocks"] = [b for b in report["blocks"] if b["block_id"] not in removed_blocks]
        report["blocks"] = [
            b
            for b in report["blocks"]
            if not (b["type"] in {"analysis", "action_plan", "metric_cards"} and not b.get("items"))
        ]
        quality = report.setdefault("quality", {})
        quality["analysis_items"] = sum(
            len(b.get("items", []))
            for b in report["blocks"]
            if b["type"] in {"analysis", "action_plan"}
        )
        quality["scope_review"] = {
            "policy_version": "public-event-v1",
            "rejected_texts": rejected,
            "incomplete_texts": incomplete,
            "incomplete_reasons": dict(
                Counter(d.reason for d in decisions if d.status == "incomplete")
            )
            if self.scope_reviewer
            else {"reviewer_unavailable": incomplete},
            "diagnostics": list(
                {
                    json.dumps(d.diagnostic, sort_keys=True): d.diagnostic
                    for d in decisions
                    if d.diagnostic
                }.values()
            )
            if self.scope_reviewer
            else [],
        }
        if rejected or incomplete:
            core_removed = any(
                owner.get("section") in {"01", "02", "04", "07"} and part - safe
                for owner, item, part in units
                if isinstance(owner, dict)
            ) or any(part - safe for owner, item, part in units if isinstance(owner, list))
            if core_removed or not report["metrics"]["key_claims_rendered"]:
                quality["release_label"] = (
                    "evidence_brief"
                    if report["metrics"]["key_claims_rendered"]
                    else "retrieval_diagnostic"
                )
            if core_removed:
                quality["release_gate_missing"] = list(
                    dict.fromkeys(
                        [
                            *quality.get("release_gate_missing", []),
                            "scope_review_incomplete" if incomplete else "scope_content_rejected",
                        ]
                    )
                )
            limits = next((b for b in report["blocks"] if b["type"] == "limitations"), None)
            if limits is None:
                limits = {
                    "block_id": "b_08_scope_limits",
                    "type": "limitations",
                    "section": "08",
                    "in_brief": True,
                    "items": [],
                }
                report["blocks"].append(limits)
            for block in report["blocks"]:
                if block["type"] == "report_header":
                    block["subtitle"] = (
                        "证据简报 · 部分内容审查未完成"
                        if quality["release_label"] == "evidence_brief"
                        else block["subtitle"]
                    )
            limits["items"].append(
                {
                    "id": "L98",
                    "category": "范围与隐私审查",
                    "text": f"明确排除 {rejected} 段内容；{incomplete} 段尚未完成审查或脱敏后核验，暂不展示。已通过审查的事实与章节保留。",
                }
            )
            reason_labels = {
                "local_budget": "本地任务额度不足",
                "call_failed": "模型调用失败",
                "upstream_unavailable": "模型服务暂时不可用",
                "invalid_output": "模型返回格式不合格",
                "input_length": "待审文本超过单次输入上限",
            }
            reason_counts = quality["scope_review"].get("incomplete_reasons", {})
            reason_text = "；".join(
                f"{reason_labels.get(reason, reason)} {count} 段"
                for reason, count in sorted(reason_counts.items())
                if count
            )
            if reason_text:
                limits["items"][-1]["text"] += " 分类原因：" + reason_text + "。"
            reasons = sorted(
                {d.get("message", "审查结果未取得") for d in quality["scope_review"]["diagnostics"]}
            )
            if reasons:
                limits["items"][-1]["text"] += (
                    " 未完成原因：" + "；".join(reasons) + "。可从已保存材料恢复审查。"
                )

    @staticmethod
    def _redact_source_labels(report, approved_labels):
        evidence_ids = {
            item["evidence_ref"]
            for block in report["blocks"]
            if block["type"] == "evidence_appendix"
            for item in block.get("items", [])
        }
        changes = 0

        def visit(value, *, material_timeline=False):
            nonlocal changes
            if isinstance(value, dict):
                refs = set(value.get("evidence_refs", []))
                refs.update(
                    value[key]
                    for key in ("evidence_ref", "evidence_id")
                    if isinstance(value.get(key), str)
                )
                if refs and refs <= evidence_ids:
                    keys = {"title", "framing", "publisher", "source_name", "publisher_entity"}
                    if material_timeline:
                        keys.add("text")  # This chart displays source titles, not claim bodies.
                    for key in keys:
                        original = value.get(key)
                        if isinstance(original, str) and original in approved_labels:
                            value[key] = approved_labels[original]
                            changes += 1
                for child in value.values():
                    visit(child, material_timeline=material_timeline)
            elif isinstance(value, list):
                for child in value:
                    visit(child, material_timeline=material_timeline)

        for block in report["blocks"]:
            if block["type"] in {"propagation_network", "history_compare"}:
                visit(block)
            elif block.get("block_id") == "b_02_correction_timeline":
                visit(block, material_timeline=True)
        return changes

    async def _scope_report_is_safe(self, report: dict[str, Any]) -> bool:
        if self.scope_reviewer is None:
            return False
        texts: set[str] = set()

        def collect(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    collect(child_value, child_key)
            elif isinstance(value, list):
                for child in value:
                    collect(child, key)
            elif (
                isinstance(value, str)
                and key not in {"url", "origin_url", "snapshot_path", "content_sha256"}
                and any("\u4e00" <= char <= "\u9fff" for char in value)
            ):
                texts.add(value)

        collect(report)
        if any(len(value) > 3000 for value in texts):
            return False
        return all(await self.scope_reviewer.accepted(sorted(texts), kind="report_text"))

    async def build(
        self, task_id: str, *, forum=(), orchestration_limitations=(), diagnostic_only=False
    ):
        task = await self.database.get_task(task_id)
        gateway = getattr(self.scope_reviewer, "gateway", None)
        cap = getattr(gateway, "token_limit", None)
        protected = task and task.investigation_scope in PROTECTED_SCOPES and cap is not None
        if protected:
            used = gateway.tokens_used
            gateway.token_limit = used + max(0, cap - used) * 3 // 4
        try:
            return await self._build(
                task_id,
                forum=forum,
                orchestration_limitations=orchestration_limitations,
                diagnostic_only=diagnostic_only,
                scope_review_cap=cap if protected else None,
            )
        finally:
            if protected:
                gateway.token_limit = cap

    async def _build(
        self,
        task_id: str,
        *,
        forum: Sequence[ForumMessage] = (),
        orchestration_limitations: Sequence[str] = (),
        diagnostic_only: bool = False,
        scope_review_cap: int | None = None,
    ) -> tuple[str, dict[str, Any], str]:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        if hasattr(self.reporter, "bind"):
            self.reporter.bind(self.database, task_id)
        report_id, report, path = await self.brief.build(task_id)
        scoped_fallback = (
            copy.deepcopy(report) if task.investigation_scope in PROTECTED_SCOPES else None
        )
        evidence = await self.database.list_evidence(task_id)
        claims = await self.database.list_claims(task_id)
        if scoped_fallback is not None:
            approved_refs = {
                item["claim_ref"]
                for block in report["blocks"]
                if block["type"] == "fact_check_table"
                for item in block["items"]
            }
            claims = [claim for claim in claims if claim.local_id in approved_refs]
        has_explicit_window = bool(task.time_range_from or task.time_range_to)
        main_evidence = [
            item
            for item in evidence
            if (item.extra or {}).get("scope_status")
            in {"main", "foreign_supplement", "event_context"}
            or (not has_explicit_window and not (item.extra or {}).get("scope_status"))
        ]
        in_window_evidence = [
            item
            for item in main_evidence
            if (item.extra or {}).get("scope_status") != "event_context"
        ]
        main_evidence_ids = {item.local_id for item in main_evidence}
        by_type = {block["type"]: block for block in report["blocks"]}
        appendix_items = by_type["evidence_appendix"]["items"]
        appendix_ids = {item["evidence_ref"] for item in appendix_items}
        evidence_by_id = {item.local_id: item for item in evidence}
        for item in evidence:
            if item.local_id in appendix_ids:
                continue
            appendix_items.append(
                {
                    "evidence_ref": item.local_id,
                    "title": item.title,
                    "source_name": item.source_name or item.publisher_entity,
                    "publisher_entity": item.publisher_entity,
                    "source_tier": item.source_tier,
                    "source_role": item.source_role,
                    "published_at": item.published_at,
                    "url": item.url,
                    "citations": [],
                    "fetch_status": item.fetch_status,
                    "snapshot_pk": item.pk if item.fetch_status == "fetched" else None,
                    "content_sha256": item.content_sha256,
                    "content_origin": (item.extra or {}).get("content_origin"),
                    "kind": item.kind,
                    "lang": item.lang or "unknown",
                    "original_excerpt": (item.content_text or item.snippet or "")[:1200],
                    "scope_status": (item.extra or {}).get("scope_status", "unclassified"),
                    "scope_label": (item.extra or {}).get("scope_label"),
                }
            )
        bound_source_ids = {ref for claim in claims for ref in claim.evidence_ids}
        for appendix in appendix_items:
            source = evidence_by_id.get(appendix.get("evidence_ref"))
            if source is None:
                continue
            appendix.setdefault("kind", source.kind)
            appendix.setdefault("lang", source.lang or "unknown")
            appendix.setdefault(
                "scope_status", (source.extra or {}).get("scope_status", "unclassified")
            )
            appendix.setdefault("scope_label", (source.extra or {}).get("scope_label"))
            appendix.setdefault("content_origin", (source.extra or {}).get("content_origin"))
            appendix.setdefault(
                "original_excerpt", (source.content_text or source.snippet or "")[:1200]
            )
            if (
                self.translator is not None
                and (source.lang or "zh").split("-", 1)[0].lower() != "zh"
                and appendix["original_excerpt"]
                and source.local_id in bound_source_ids
                and not appendix.get("machine_translation_zh")
            ):
                try:
                    appendix["machine_translation_zh"] = await self.translator.translate_to_chinese(
                        appendix["original_excerpt"], source.lang or "unknown"
                    )
                except Exception:
                    appendix["translation_status"] = "unavailable"
        report["metrics"].update(
            evidence_total=len(evidence),
            evidence_fetched=sum(item.fetch_status == "fetched" for item in evidence),
            evidence_snippet_only=sum(item.fetch_status != "fetched" for item in evidence),
            independent_publishers=len(
                {
                    item.publisher_entity or item.source_name or item.source_domain
                    for item in evidence
                }
            ),
            time_span_days=self._time_span_days(evidence),
            main_evidence_total=len(in_window_evidence),
            event_context_evidence_total=len(main_evidence) - len(in_window_evidence),
            background_evidence_total=len(evidence) - len(main_evidence),
        )
        header = by_type["report_header"]
        if task.investigation_scope not in PROTECTED_SCOPES:
            header["event_title"] = task.resolved_event_query or task.event_query
        report["task"]["original_query"] = task.event_query
        report["task"]["resolved_event_query"] = task.resolved_event_query
        snapshot = json.loads(task.config_snapshot) if task.config_snapshot else {}
        header["models_used"] = snapshot.get("models_used", {})

        for index, text in enumerate(orchestration_limitations, start=10):
            by_type["limitations"]["items"].append(
                {"id": f"L{index:02d}", "category": "协作编排", "text": text}
            )
        if not orchestration_limitations:
            by_type["limitations"]["items"].append(
                {
                    "id": "L03",
                    "category": "方法边界",
                    "text": "本报告仅描述已检索到的公开材料，不预测事件未来走向。",
                }
            )
        models = snapshot.get("models_used", {})
        if models and models.get("verifier") == models.get("analyst_a"):
            by_type["limitations"]["items"].append(
                {
                    "id": "L05",
                    "category": "核验独立性",
                    "text": "核验模型与分析模型同源，本次交叉核验强度降低。",
                }
            )

        historical_claims = {
            claim.local_id: claim for claim in claims if claim.agent == "history_insight"
        }
        historical_facts = [
            item
            for item in by_type["fact_check_table"]["items"]
            if item.get("origin_agent") == "history_insight"
            and item.get("verification_state") == "complete"
            and item.get("badge") != "refuted"
            and (claim := historical_claims.get(item["claim_ref"])) is not None
            and bool(
                independent_case_evidence(
                    claim,
                    evidence_by_id,
                    task.resolved_event_query or task.event_query,
                    task.created_at,
                )
                & {
                    c["evidence_ref"]
                    for c in item.get("citations", [])
                    if c.get("relation") == "support"
                }
            )
        ]
        fact_items = [
            item
            for item in by_type["fact_check_table"]["items"]
            if item.get("origin_agent") != "history_insight"
        ]
        # 历史对照只进入第 06 章的结构化案例卡；不得混入当前事件事实核查表。
        by_type["fact_check_table"]["items"] = fact_items
        primary_fact_items = [
            item
            for item in fact_items
            if item.get("origin_agent") != "history_insight"
            and any(
                citation.get("evidence_ref") in main_evidence_ids
                for citation in item.get("citations", [])
            )
        ]
        rendered_claim_ids = {item["claim_ref"] for item in fact_items}
        rendered_claims = [claim for claim in claims if claim.local_id in rendered_claim_ids]
        cited_evidence_ids = {
            citation["evidence_ref"]
            for item in fact_items
            for citation in item.get("citations", [])
        }
        relation_rows = {
            claim.local_id: await self.database.claim_evidence_rows(claim.pk)
            for claim in rendered_claims
        }
        timeline_sources: dict[str, dict[str, Any]] = {}
        for claim in rendered_claims:
            if claim.agent == "history_insight":
                continue
            for row in relation_rows[claim.local_id]:
                if (
                    not row["published_at"]
                    or row["evidence_id"] not in cited_evidence_ids
                    or row["evidence_id"] not in main_evidence_ids
                    or row["relation"] not in {"support", "partial", "contradict", "conflict"}
                ):
                    continue
                entry = timeline_sources.setdefault(
                    row["evidence_id"],
                    {
                        "date": row["published_at"],
                        "text": row["title"],
                        "source_name": row["source_name"] or row["publisher_entity"],
                        "window_label": (
                            "重点窗口外的本事件材料"
                            if (evidence_by_id[row["evidence_id"]].extra or {}).get("scope_status")
                            == "event_context"
                            else None
                        ),
                        "evidence_refs": [row["evidence_id"]],
                        "claim_refs": [],
                        "relations": Counter(),
                    },
                )
                entry["claim_refs"].append(claim.local_id)
                entry["relations"][row["relation"] or "unverified"] += 1
        timeline_items = []
        dated = sorted(timeline_sources.values(), key=lambda value: value["date"])
        # 长时段保留首尾，不能只截最早材料而漏掉最新进展。
        selected_dates = select_priority_timeline_nodes(dated)
        for item in selected_dates:
            item["claim_refs"] = sorted(set(item["claim_refs"]))
            item["relations"] = dict(item["relations"])
            timeline_items.append(item)
        timeline = {
            "block_id": "b_02_correction_timeline",
            "type": "chart",
            "section": "02",
            "in_brief": False,
            "title": "关键材料发布脉络",
            "data_basis": "claim_evidence_database",
            "chart_kind": "timeline",
            "items": timeline_items,
            "fallback_text": "被引用材料缺少可用发布日期，无法构建可靠的纠偏时间线。"
            if not timeline_items
            else None,
            "note": "按被引用材料的发布日期排列；窗口外材料单独标明。发布日期不等于事件发生日期或首发时间；仅显示有核验关联的材料，不代表全网声量。",
        }

        total_claims = len(fact_items)
        verified_claims = sum(item["badge"] == "verified" for item in fact_items)
        unverified_claims = sum(item["badge"] == "unverified" for item in fact_items)
        single_source_supported = sum(
            item["badge"] == "unverified"
            and item["verification_state"] == "complete"
            and item.get("independent_sources") == 1
            and item.get("evidence_grade") == "fulltext"
            and any(citation.get("relation") == "support" for citation in item.get("citations", []))
            for item in fact_items
        )
        report["metrics"]["single_source_supported_count"] = single_source_supported
        cited_records = [
            evidence_by_id[evidence_id]
            for evidence_id in sorted(cited_evidence_ids)
            if evidence_id in evidence_by_id
        ]
        cited_attempted = sum(item.fetch_status != "discovered" for item in cited_records)
        cited_fetched = sum(item.fetch_status == "fetched" for item in cited_records)
        report["metrics"]["cited_evidence_total"] = len(cited_records)
        # "核验未完成"与"核验后没有支持"同属待核验，但成因不同，KPI 上必须说清是哪一种，
        # 否则一次上游故障会被读成"这批材料没有依据"。
        incomplete_claims = report["metrics"].get("key_claims_verification_incomplete", 0)
        skipped_claims = report["metrics"].get("key_claims_verification_skipped", 0)
        unfinished_claims = incomplete_claims + skipped_claims
        unverified_note = (
            f"其中 {unfinished_claims} 条为核验未完成（上游不可用或预算截断），不代表材料无依据"
            if unfinished_claims
            else "需要更多独立支持或一手材料"
        )
        unresolved_claims = max(0, unverified_claims - single_source_supported)
        kpis = {
            "block_id": "b_00_kpi",
            "type": "kpi_grid",
            "section": "09",
            "in_brief": False,
            "data_basis": "evidence_database",
            "items": [
                {
                    "label": "已证实陈述",
                    "value": f"{verified_claims} / {total_claims}",
                    "note": "描述核验结论，不代表系统运行成功率",
                    "tone": "verified" if verified_claims else "neutral",
                },
                {
                    "label": "待核验陈述",
                    "value": f"{unresolved_claims} / {total_claims}",
                    "note": unverified_note,
                    "tone": "warning" if unresolved_claims else "neutral",
                },
                {
                    "label": "来源直接支持",
                    "value": f"{single_source_supported} / {total_claims}",
                    "note": "已取得原文并支持整条陈述；仍需独立互证才会标为已证实",
                    "tone": "neutral" if single_source_supported else "warning",
                },
                {
                    "label": "已取得原文",
                    "value": f"{report['metrics']['evidence_fetched']} / {len(evidence)}",
                    "note": "其余材料仅有搜索摘要或抓取失败",
                    "tone": "verified" if report["metrics"]["evidence_fetched"] else "warning",
                },
                {
                    "label": "实际引用材料",
                    "value": f"{len(cited_records)} / {len(evidence)}",
                    "note": "未引用材料不参与关键陈述结论",
                    "tone": "neutral",
                },
            ],
        }
        evidence_funnel = {
            "block_id": "b_04_evidence_funnel",
            "type": "chart",
            "section": "09",
            "in_brief": False,
            "title": "证据获取漏斗",
            "data_basis": "evidence_database",
            "chart_kind": "funnel",
            "items": [
                {"label": "去重检索材料", "value": len(evidence)},
                {"label": "实际引用材料", "value": len(cited_records)},
                {"label": "引用材料已尝试取原文", "value": cited_attempted},
                {"label": "引用材料已存原文", "value": cited_fetched},
            ],
            "note": "漏斗只衡量证据获取完整度，不把检索数量当作事件声量。",
        }
        fact_by_id = {item["claim_ref"]: item for item in fact_items}
        matrix_items = []
        for claim in rendered_claims:
            relations = Counter(
                (row["relation"] or "unverified")
                for row in relation_rows[claim.local_id]
                if row["evidence_id"] in cited_evidence_ids
            )
            fact = fact_by_id[claim.local_id]
            matrix_items.append(
                {
                    "claim_ref": claim.local_id,
                    "text": claim.text,
                    "badge": fact["badge"],
                    "independent_sources": fact.get("independent_sources", 0),
                    "relations": dict(relations),
                }
            )
        verification_matrix = {
            "block_id": "b_04_verification_matrix",
            "type": "chart",
            "section": "09",
            "in_brief": False,
            "title": "关键陈述—信源核验矩阵",
            "data_basis": "claim_evidence_database",
            "chart_kind": "matrix",
            "items": matrix_items,
            "note": "同源转载按发布主体归并；部分支持和未提及不计为独立支持。",
        }
        hot_points = await self.historical_data.hotlist_query(
            task.event_query,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        rendered_bindings = {
            item["claim_ref"]: {citation["evidence_ref"] for citation in item.get("citations", [])}
            for item in fact_items
        }
        publication_network, publication_nodes, propagation_edges = self._publication_network(
            claims,
            evidence_by_id,
            main_evidence_ids,
            rendered_bindings,
            by_type["limitations"],
            task.resolved_event_query or task.event_query,
        )
        if not diagnostic_only and hasattr(self.reporter, "recover_relations"):
            nodes_by_id = {n["evidence_id"]: n for n in publication_network["nodes"]}
            source_context = self._relation_source_context(main_evidence, nodes_by_id)
            fingerprint = hashlib.sha256(
                json.dumps(source_context, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            prior_relations = await self.database.checkpoint(task_id, "report:relations") or {}
            source_fingerprints = {
                s["evidence_ref"]: hashlib.sha256(
                    json.dumps(s, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                for s in source_context
            }
            if (
                prior_relations.get("fingerprint") == fingerprint
                and prior_relations.get("status") == "complete"
            ):
                recovered = prior_relations.get("edges", [])
                diagnostics = prior_relations.get("diagnostics", [])
            else:
                recovered = await self.reporter.recover_relations(source_context)
                diagnostics = getattr(self.reporter, "relation_diagnostics", [])
                retained = [
                    e
                    for e in prior_relations.get("edges", [])
                    if e.get("review_status") == "accepted"
                    and all(
                        source_fingerprints.get(ref) is not None
                        and source_fingerprints[ref]
                        == prior_relations.get("source_fingerprints", {}).get(ref)
                        for ref in {
                            e["from_evidence_id"],
                            e["to_evidence_id"],
                            e.get("support_evidence_id"),
                        }
                    )
                ]
                recovered = list(
                    {
                        (e["from_evidence_id"], e["to_evidence_id"], e["relation"]): e
                        for e in [*retained, *recovered]
                    }.values()
                )
                await self.database.save_checkpoint(
                    task_id,
                    "report:relations",
                    {
                        "phase": "verified",
                        "fingerprint": fingerprint,
                        "version": 2,
                        "source_fingerprints": source_fingerprints,
                        "candidates": getattr(self.reporter, "relation_candidates", []),
                        "edges": recovered,
                        "diagnostics": diagnostics,
                        "status": "partial"
                        if any(d.get("category") != "review_rejected" for d in diagnostics)
                        else "complete",
                    },
                )
            publication_network["diagnostics"] = diagnostics
            eligible_sources = {item["evidence_ref"] for item in source_context}
            recovered = [
                edge
                for edge in recovered
                if edge.get("review_status") == "accepted"
                and edge.get("from_evidence_id") in eligible_sources
                and edge.get("to_evidence_id") in eligible_sources
                and edge.get("support_evidence_id") in eligible_sources
            ]
            for edge in recovered:
                for evidence_id, node_type in (
                    (edge["from_evidence_id"], "original"),
                    (edge["to_evidence_id"], "repost"),
                ):
                    if evidence_id in nodes_by_id:
                        continue
                    source = evidence_by_id[evidence_id]
                    node = {
                        "evidence_id": evidence_id,
                        "publisher": source.publisher_entity
                        or source.source_name
                        or source.source_domain,
                        "published_at": source.published_at,
                        "node_type": node_type,
                        "framing": (source.title or "发布记录")[:300],
                        "evidence_refs": [evidence_id],
                        "review_status": "accepted",
                    }
                    nodes_by_id[evidence_id] = node
                    publication_network["nodes"].append(node)
            publication_network["edges"] = recovered
            publication_nodes = len(publication_network["nodes"])
            propagation_edges = len(recovered)
            if not recovered:
                publication_network["fallback_text"] = (
                    "已列出可回查的发布记录；尚未证明这些节点之间的转载或回应关系。"
                )
            if recovered:
                publication_network["fallback_text"] = None
                publication_network["in_brief"] = True
                publication_network["data_basis"] = "typed_claim_and_reviewed_relation_evidence"
                by_type["limitations"]["items"] = [
                    item for item in by_type["limitations"]["items"] if item.get("id") != "L16"
                ]
        propagation = [publication_network, evidence_funnel, verification_matrix]
        numeric_hot_points = [item for item in hot_points if item.heat_value is not None]
        if hot_points and not numeric_hot_points:
            by_type["limitations"]["items"].append(
                {
                    "id": "L15",
                    "category": "热榜热度",
                    "text": "热榜命中记录缺少可比较的数值热度，未绘制热度曲线。",
                }
            )
        if numeric_hot_points:
            propagation.append(self._hot_chart(numeric_hot_points))

        role_labels = {
            "authority": "裁判性权威",
            "party": "事件当事方",
            "independent": "独立采编",
            "syndicated": "转载",
            "unknown": "未分类",
        }
        role_distribution = Counter(
            role_labels.get(item.source_role, item.source_role) for item in evidence
        )
        date_distribution = Counter(
            item.published_at[:10] if item.published_at else "日期未知" for item in evidence
        )
        publisher_distribution = Counter(
            item.publisher_entity or item.source_name or item.source_domain for item in evidence
        )
        # 被丢弃的证据不计入独立信源，但必须让读者看见丢了多少、为什么丢——
        # 否则 verified_rate 为 0 时无法判断是"证据不支持"还是"来源没被识别"。
        stance_drops = report["metrics"].get("stance_drops", {})
        drop_summary = [
            {"label": label, "value": stance_drops[reason]}
            for reason, label in (
                ("unknown", "未识别来源未计入独立信源"),
                ("syndicated", "转载来源未计入独立信源"),
                ("party", "当事方声明未计入独立信源"),
                ("not_mentioned", "材料未提及该陈述"),
            )
            if stance_drops.get(reason)
        ]
        data_quality = {
            "block_id": "b_09_data_quality",
            "type": "data_quality",
            "section": "09",
            "in_brief": False,
            "title": "核验方法与数据质量",
            "summary": [
                {"label": "未分类信源", "value": role_distribution.get("未分类", 0)},
                {"label": "缺少发布日期", "value": date_distribution.get("日期未知", 0)},
                {
                    "label": "原文抓取失败",
                    "value": sum(item.fetch_status == "fetch_failed" for item in evidence),
                },
                {"label": "未被关键陈述引用", "value": len(evidence) - len(cited_records)},
                *drop_summary,
            ],
            "verification_method": {
                "verified_rate": report["metrics"]["verified_rate"],
                "single_source_supported_count": report["metrics"].get(
                    "single_source_supported_count", 0
                ),
                "weighted_verified_rate": report["metrics"]["weighted_verified_rate"],
                "weight_scheme": report["metrics"]["weight_scheme"],
            },
            "distributions": [
                {
                    "title": "检索材料日期分布",
                    "items": [
                        {"label": label, "value": value}
                        for label, value in sorted(date_distribution.items())
                    ],
                },
                {
                    "title": "检索材料信源类型",
                    "items": [
                        {"label": label, "value": value}
                        for label, value in sorted(role_distribution.items())
                    ],
                },
                {
                    "title": "检索材料来源主体（前 12）",
                    "items": [
                        {"label": label, "value": value}
                        for label, value in publisher_distribution.most_common(12)
                    ],
                },
            ],
        }
        viewpoint = {
            "block_id": "b_05_viewpoints",
            "type": "viewpoint_list",
            "section": "05",
            "in_brief": False,
            "title": "议题分析的数据条件",
            "items": [],
            "fallback_text": "尚未形成通过引用检查的综合议题分析；已有事实见核查表，不能从调查席位发言推算公众立场或情感比例。",
        }
        comment_rows = await self.database.fetch_all(
            """SELECT s.platform,c.status,c.collected_count,c.sampling_method,c.error,s.url,s.title
               FROM comment_collection c JOIN social_candidate s ON s.id=c.candidate_id
               WHERE c.task_id=? ORDER BY c.started_at""",
            (task_id,),
        )
        comment_messages = [item for item in forum if item.agent == "comment_insight"]
        comment_insight = {
            "block_id": "b_05_comment_insight",
            "type": "comment_insight",
            "section": "05",
            "in_brief": False,
            "title": "确认帖子评论样本洞察",
            "sample_notice": "仅代表用户确认帖子的已采集样本，不代表平台整体或全网民意。",
            "collections": [dict(item) for item in comment_rows],
            "items": [
                {
                    "text": item.content,
                    "evidence_refs": [ref for ref in item.refs if ref.startswith("E")],
                    "sampling_scope": item.payload.get("sampling_scope"),
                }
                for item in comment_messages
            ],
            "fallback_text": "本任务未采集登录态评论，报告仅基于公开搜索材料。"
            if not comment_rows
            else None,
        }
        structured_comments = next(
            (
                (m.payload or {}).get("comment_analysis")
                for m in reversed(comment_messages)
                if (m.payload or {}).get("comment_analysis")
            ),
            None,
        )
        if structured_comments:
            comment_insight.update(
                items=structured_comments.get("items", []),
                samples=structured_comments.get("samples", []),
                coverage=structured_comments.get("coverage", {}),
                warnings=structured_comments.get("warnings", []),
                diagnostics=structured_comments.get("diagnostics", []),
                analysis_status=structured_comments.get("status", "unknown"),
                analysis_version=structured_comments.get("version", 1),
                fallback_text=None
                if structured_comments.get("items")
                else "未形成通过原始样本审查的主题；采集和分类覆盖见下方记录。",
            )
        evidence_counts = Counter(item.lang or "unknown" for item in evidence)
        available_languages = {language for language, count in evidence_counts.items() if count > 0}
        requested = task.source_languages
        report["language_coverage"] = {
            "requested": task.source_languages,
            "complete": [
                item for item in requested if item in {"zh", "en"} and item in available_languages
            ],
            "best_effort": [
                item
                for item in requested
                if item not in {"zh", "en"} and item in available_languages
            ],
            "missing": [item for item in requested if item not in available_languages],
            "evidence_counts": dict(evidence_counts),
            "report_language": "zh-CN",
        }
        local_history = await self.historical_data.task_matches(task_id)
        local_cards = [
            {
                "event_name": item.event_name,
                "case_type": "analogous",
                "event_time": item.event_time_start,
                "summary": item.summary,
                "outcome": item.dimensions["最终结局"],
                "comparison": f"相似点：{('、'.join(sorted(item.matched_terms)) or '结构化维度相近')}。关键差异：事件主体与发生时间不同，不能据此预测本事件走向。",
                "dimensions": item.dimensions,
                "provenance": item.provenance,
                "evidence_refs": [item.evidence_id],
            }
            for item in local_history
        ]
        for item in historical_facts:
            claim = historical_claims[item["claim_ref"]]
            case = claim.analysis_data["historical_case"]
            eligible_refs = independent_case_evidence(
                claim,
                evidence_by_id,
                task.resolved_event_query or task.event_query,
                task.created_at,
            )
            supported_refs = [
                citation["evidence_ref"]
                for citation in item.get("citations", [])
                if citation.get("relation") == "support"
                and citation["evidence_ref"] in eligible_refs
            ]
            if not supported_refs:
                continue
            source = evidence_by_id[supported_refs[0]]
            status = "已核验陈述" if item.get("badge") == "verified" else "来源记载，独立核实不足"
            local_cards.append(
                {
                    "event_name": case["name"],
                    "case_type": case.get("case_type", "analogous"),
                    "event_time": f"资料发布：{str(source.published_at)[:10]}",
                    "summary": f"{status}：{item['text']}",
                    "outcome": case["outcome"],
                    "comparison": (
                        (
                            f"可核查关联：{case.get('connection', '')}。"
                            if case.get("case_type") == "related_prior"
                            else ""
                        )
                        + f"相似机制：{case['similarity']}。"
                        f"关键差异：{case['difference']}。"
                        "该案例不能预测当前事件走向。"
                    ),
                    "dimensions": {
                        "公开结果（来源记载）": case["outcome"],
                        "核验状态": status,
                    },
                    "provenance": source.source_name or source.source_domain or "来源待核",
                    "claim_ref": item["claim_ref"],
                    "evidence_refs": supported_refs,
                }
            )
        local_cards = unique_history_cards(local_cards)
        accepted_history_refs = {
            citation["evidence_ref"]
            for item in historical_facts
            for citation in item.get("citations", [])
        }
        considered_history_refs = {
            item.local_id
            for item in evidence
            if (item.extra or {}).get("scope_status") == "history"
        }
        excluded_history = []
        for source in evidence:
            if (
                source.local_id not in considered_history_refs
                or source.local_id in accepted_history_refs
                or (source.extra or {}).get("scope_status") != "history"
            ):
                continue
            if not source.published_at or (source.extra or {}).get("date_provenance") not in {
                "page_metadata",
                "page_visible",
                "trusted_structured",
                "user_provided",
            }:
                reason = (
                    "待核实历史案例线索：来源发布日期未从原网页确认，不能判断观察时点是否已公开"
                )
            elif str(source.published_at)[:10] > task.created_at[:10]:
                reason = "公开时间晚于本任务启动日"
            elif (task.resolved_event_query or task.event_query) in (
                f"{source.title or ''} {(source.content_text or '')[:1600]}"
            ):
                reason = "仍属本事件报道，不能充当独立对照"
            elif source.local_id not in {
                ref
                for claim in claims
                if claim.agent == "history_insight"
                for ref in claim.evidence_ids
            }:
                reason = "已取得来源，但尚未形成通过核验的独立案例陈述"
            else:
                reason = "尚未建立独立案例身份、相似机制和公开结果的完整依据"
            excluded_history.append(
                {
                    "title": source.title,
                    "published_at": source.published_at,
                    "reason": reason,
                    "evidence_refs": [source.local_id],
                }
            )
        excluded_history.sort(
            key=lambda item: (
                0
                if item["reason"].startswith("已取得来源")
                else 1
                if item["reason"].startswith("尚未建立")
                else 2
                if item["reason"].startswith("仍属")
                else 3
                if item["reason"].startswith("公开时间")
                else 4,
                item["published_at"] or "",
            )
        )
        history = {
            "block_id": "b_06_history",
            "type": "history_compare",
            "section": "06",
            "in_brief": False,
            "title": "历史对照",
            "cards": local_cards,
            "excluded_candidates": excluded_history[:8],
            "fallback_text": (
                f"已复查 {len(excluded_history)} 份历史席位引用材料，尚无通过独立事件、时间与可比机制审查的案例；"
                "下列材料说明排除原因，不能把本事件旧闻或任务启动后才公开的结果写成历史比较。"
                if excluded_history
                else "未取得通过可比性审查的独立历史对照；需补充任务启动时已公开的原始通报或裁判文书。"
            )
            if not local_cards
            else None,
        }
        recommendations = {
            "block_id": "b_07_recommendations",
            "type": "text",
            "section": "07",
            "in_brief": False,
            "title": "研判与建议",
            "fallback_text": "本轮未形成有充分依据的行动研判。请先补齐核心回应原文与当前进展，再决定处置重点；当前产物仅可作为证据简报。",
        }

        analysis_facts = [
            item
            for item in primary_fact_items
            if item.get("verification_state") == "complete"
            and (
                item.get("badge") == "verified"
                or (
                    item.get("badge") == "unverified"
                    and any(c.get("relation") == "support" for c in item.get("citations", []))
                )
            )
        ]
        analysis_facts.extend(historical_facts)
        analysis_source_ids = {
            citation["evidence_ref"]
            for item in analysis_facts
            for citation in item.get("citations", [])
        }
        analysis_sources = [item for item in evidence if item.local_id in analysis_source_ids]
        enrichment: dict[str, Any] = {}
        if self.reporter is not None and not diagnostic_only:
            try:
                context = report_context(report["task"], analysis_facts, analysis_sources, forum)
                context["comment_insights"] = comment_insight.get("items", [])
                context["propagation_edges"] = propagation_edges
                if hasattr(self.reporter, "bind"):
                    self.reporter.bind(self.database, task_id)
                enrichment = await self.reporter.enrich(context)
                if not isinstance(enrichment, dict):
                    enrichment = {}
                warnings = enrichment.get("section_warnings") or []
                for warning in warnings[:8] if isinstance(warnings, list) else []:
                    if isinstance(warning, str) and warning.strip():
                        by_type["limitations"]["items"].append(
                            {
                                "id": f"L9{len(by_type['limitations']['items'])}",
                                "category": "报告 Agent 提醒",
                                "text": warning.strip()[:500],
                            }
                        )
            except Exception as exc:
                by_type["limitations"]["items"].append(
                    {
                        "id": "L90",
                        "category": "报告生成",
                        "text": f"报告 Agent 降级：{type(exc).__name__}",
                    }
                )
        summary, analysis_blocks, quality = assemble_analysis(
            enrichment,
            analysis_facts,
            analysis_sources,
            propagation_edges=propagation_edges,
        )
        if (
            not diagnostic_only
            and quality["rejected_items"]
            and not summary["so_what"]
            and hasattr(self.reporter, "repair_actions")
        ):
            original_rejections = dict(quality["rejected_items"])
            try:
                repaired = await self.reporter.repair_actions(context, original_rejections)
                if repaired.get("analyses"):
                    enrichment["analyses"] = [
                        a for a in enrichment.get("analyses", []) if a.get("section") != "07"
                    ] + repaired["analyses"]
                    summary, analysis_blocks, quality = assemble_analysis(
                        enrichment,
                        analysis_facts,
                        analysis_sources,
                        propagation_edges=propagation_edges,
                    )
                quality["action_repair"] = {
                    "status": "accepted" if summary["so_what"] else "rejected",
                    "initial_rejections": original_rejections,
                }
            except Exception as exc:
                quality["action_repair"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "initial_rejections": original_rejections,
                }
        event_nodes = event_timeline(
            primary_fact_items,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        if len(event_nodes) >= 2:
            timeline.update(
                title="关键事件与机构回应时点",
                items=event_nodes,
                fallback_text=None,
                note="时点仅取自陈述正文明确写出的年月日；窗口外前史与后续进展单独标明。徽章保留陈述核验状态，不以网页发布日期替代事件日期。同日最多展示两个节点。",
            )
        if not summary["what"]:
            # A diagnostic still explains which attributed records were obtained.
            summary["what"] = [
                {"claim_ref": item["claim_ref"], "text": item["text"]}
                for item in distinct_facts(primary_fact_items)
            ]
        by_type["executive_summary"].update(summary)
        if not any(item.get("badge") == "verified" for item in primary_fact_items):
            by_type["executive_summary"]["lede"] = (
                "以下列出已取得来源中的主要记载及其核验状态；目前尚不足以确认为事件事实，"
                "分析仅在明确的证据和条件范围内成立。"
            )
        by_type["fact_check_table"]["priority_claim_refs"] = [
            item["claim_ref"] for item in summary["what"]
        ]
        definitive_claims = sum(
            item.get("verification_state") == "complete"
            and item.get("badge") in {"verified", "disputed", "refuted"}
            for item in primary_fact_items
        )
        recommendation_count = sum(
            bool(item.get("evidence_refs"))
            for block in analysis_blocks
            if block.get("type") in {"analysis", "action_plan"} and block.get("section") == "07"
            for item in block.get("items", [])
        )
        release = ReportReleaseAssessment.evaluate(
            concrete_event=bool(task.resolved_event_query or task.request_kind == "event"),
            main_evidence=len(main_evidence),
            verifiable_key_claims=definitive_claims,
            event_timeline_nodes=len(event_nodes),
            publication_nodes=publication_nodes,
            propagation_edges=propagation_edges,
            summary_has_what=bool(summary["what"]),
            summary_has_why=bool(summary["why"]),
            summary_has_action=bool(summary["so_what"]),
            evidence_bound_recommendations=recommendation_count,
        )
        if (
            release.label == "retrieval_diagnostic"
            and not diagnostic_only
            and any(
                item.get("verification_state") == "complete"
                and any(c.get("relation") == "support" for c in item.get("citations", []))
                for item in primary_fact_items
            )
        ):
            release = ReportReleaseAssessment("evidence_brief", release.missing)
        history_ready = bool(local_cards) or any(b.get("section") == "06" for b in analysis_blocks)
        comment_coverage = comment_insight.get("coverage", {})
        comments_ready = await self.database.checkpoint(task_id, "comments:ready")
        comments_skipped = bool(comments_ready is not None and not comment_rows)
        comment_status = (
            "disabled"
            if task.comment_mode == "off" or comments_skipped
            else "partial"
            if comment_insight.get("items")
            and (
                comment_coverage.get("unclassified", 0)
                or comment_coverage.get("relevant_without_reviewed_theme", 0)
            )
            else "complete"
            if comment_insight.get("items") or comment_insight.get("analysis_status") == "complete"
            else "failed"
        )
        quality["chapter_status"] = {
            "history": {
                "status": "complete" if history_ready else "missing",
                "message": "已形成可回查的独立案例"
                if history_ready
                else "未形成合格独立案例，已保留候选及排除原因",
            },
            "comments": {
                "status": comment_status,
                "message": "用户已跳过新评论采集"
                if comments_skipped
                else "未启用评论调查"
                if comment_status == "disabled"
                else "评论仅代表已采集样本；覆盖与未完成部分见评论章节",
                "coverage": comment_coverage,
            },
        }
        quality["investigation_outcome"] = await self.database.checkpoint(
            task_id, "investigation:outcome"
        ) or {"end_reason": "unknown"}
        if diagnostic_only:
            release = ReportReleaseAssessment("retrieval_diagnostic", release.missing)
        quality["release_label"] = release.label
        quality["release_gate_missing"] = list(release.missing)
        report["quality"] = quality
        header["subtitle"] = {
            "full_report": "完整舆情专报 · 事实、传播与行动依据分层呈现",
            "evidence_brief": "证据简报 · 尚未通过完整舆情专报发布门",
            "retrieval_diagnostic": "检索诊断 · 当前材料不足以生成舆情专报",
        }[release.label]
        analysis_sections = {
            block["section"]
            for block in analysis_blocks
            if block["type"] in {"analysis", "action_plan"}
        }
        if quality["rejected_items"]:
            by_type["limitations"]["items"].append(
                {
                    "id": "L91",
                    "category": "分析质量检查",
                    "text": f"已移除 {sum(quality['rejected_items'].values())} 项不符合质量检查的内容或越权字段，涉及引用对应关系、分析要素或数字依据。具体检查记录保存在报告数据中。",
                }
            )
        notice = {
            "block_id": "b_00_reading_scope",
            "type": "text",
            "section": "00",
            "in_brief": True,
            "title": "阅读范围与决策边界",
            "text": f"调查主题：{header['event_title']}。"
            + (
                f"优先调查时间窗口：{task.time_range_from or '未指定起点'} 至 {task.time_range_to or '未指定终点'}；必要的本事件前史、后续进展与独立对照另行标注。"
                if has_explicit_window
                else "未指定调查时间范围；按证据核验结果呈现本事件经过及独立历史对照。"
            )
            + (
                "已通过完整专报发布门；分析判断与已核验事实分开呈现，仍需关注各条不确定性。"
                if release.label == "full_report"
                else (
                    "当前仅输出检索诊断，没有足够的本事件证据与可核验关键陈述。"
                    if release.label == "retrieval_diagnostic"
                    else "已形成有依据的分析条目，但当前仍是证据简报；事实链、传播关系或行动依据尚不完整。"
                )
            ),
        }
        if not quality["sourced_measurements"]:
            analysis_blocks.append(
                {
                    "block_id": "b_04_numeric_gap",
                    "type": "text",
                    "section": "04",
                    "in_brief": False,
                    "title": "量化数据缺口",
                    "text": "本轮没有同时满足原文可回查、已绑定事件事实、数字与单位明确的披露指标。要判断传播规模，需要补充带采集时点与平台口径的阅读/互动数据；要比较立场，需要可说明采样方式的评论样本。检索材料数量不能替代这些指标。",
                }
            )
        by_type["limitations"]["items"] = deduplicate_limitations(by_type["limitations"]["items"])
        for item in appendix_items:
            item["cited_in_report"] = item["evidence_ref"] in cited_evidence_ids
            item["measurement_quotes"] = [
                m["quote"]
                for b in analysis_blocks
                if b["type"] == "metric_cards"
                for m in b["items"]
                if item["evidence_ref"] in m["evidence_refs"]
            ]
        report["blocks"] = [
            header,
            notice,
            kpis,
            by_type["executive_summary"],
            timeline,
            by_type["fact_check_table"],
            *propagation,
            *analysis_blocks,
            *([] if "05" in analysis_sections else [viewpoint]),
            comment_insight,
            *(
                [
                    {
                        "block_id": "b_06_basis",
                        "type": "historical_facts",
                        "section": "06",
                        "in_brief": False,
                        "title": "历史案例依据（与当前事件分开）",
                        "items": historical_facts,
                    }
                ]
                if historical_facts
                else []
            ),
            *([history] if local_cards or "06" not in analysis_sections else []),
            *([] if "07" in analysis_sections else [recommendations]),
            by_type["limitations"],
            data_quality,
            by_type["evidence_appendix"],
        ]
        if scoped_fallback is not None:
            if scope_review_cap is not None:
                self.scope_reviewer.gateway.token_limit = scope_review_cap
            self._hide_scoped_source_text(report)
            await self._retain_reviewed_blocks(report, task)
            for block in report["blocks"]:
                if block["type"] == "evidence_appendix":
                    for item in block["items"]:
                        item["measurement_quotes"] = [
                            m["quote"]
                            for b in report["blocks"]
                            if b["type"] == "metric_cards"
                            for m in b["items"]
                            if item["evidence_ref"] in m["evidence_refs"]
                        ]
        diagnostics = await self.database.llm_diagnostics(task_id)
        report.setdefault("quality", {})["call_diagnostics"] = {
            k: v for k, v in diagnostics.items() if k != "calls"
        }
        report["quality"]["timing"] = await task_timing(self.database, task_id)
        prune_citation_backlinks(report)
        validated = validate_report(report).report
        await asyncio.to_thread(
            Path(path).write_text, render_html(validated, view="full"), encoding="utf-8"
        )
        return report_id, validated, path

    @staticmethod
    def _relation_source_context(
        main_evidence: Sequence[Any], nodes_by_id: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Include bounded, dated source-credit pages outside existing media claims."""

        selected = [
            item
            for item in main_evidence
            if item.local_id in nodes_by_id and item.fetch_status == "fetched"
        ]
        publisher_names = {
            name
            for item in selected
            for name in (item.publisher_entity, item.source_name)
            if name and len(name) >= 2
        }
        extras = [
            item
            for item in main_evidence
            if item.local_id not in nodes_by_id
            and item.fetch_status == "fetched"
            and item.published_at
            and re.search(
                r"来源\s*[：:|｜]|新华社.{0,8}日电|转载自|据[^\n。；，]{2,24}(?:消息|报道)",
                (item.content_text or "")[:500],
            )
        ]
        extras.sort(
            key=lambda item: (
                not any(name in (item.content_text or "")[:500] for name in publisher_names),
                item.source_tier or 9,
                item.local_id,
            )
        )
        credited = extras[:8]
        credited_text = "\n".join((item.content_text or "")[:500] for item in credited)
        existing_ids = {item.local_id for item in [*selected, *credited]}
        originals = [
            item
            for item in main_evidence
            if item.local_id not in existing_ids
            and item.fetch_status == "fetched"
            and item.published_at
            and any(
                name and len(name) >= 2 and name in credited_text
                for name in (item.publisher_entity, item.source_name)
            )
        ][:4]
        return [
            {
                "evidence_ref": item.local_id,
                "title": item.title,
                "publisher": item.publisher_entity or item.source_name or item.source_domain,
                "url": item.url,
                "published_at": item.published_at,
                "excerpt": (item.content_text or "")[:1600],
            }
            for item in [*selected, *credited, *originals]
        ]

    @staticmethod
    def _publication_network(
        claims: Sequence[Any],
        evidence_by_id: dict[str, Any],
        main_evidence_ids: set[str],
        rendered_bindings: dict[str, set[str]],
        limitations: dict[str, Any],
        event_query: str = "",
    ) -> tuple[dict[str, Any], int, int]:
        institution_markers = re.findall(
            r"[\u4e00-\u9fff]{2,10}(?:大学|学院|医院|政府|公司|集团)", event_query
        )
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        for claim in claims:
            if claim.agent != "media_propagation":
                continue
            data = claim.analysis_data if isinstance(claim.analysis_data, dict) else {}
            node = data.get("publication_node")
            if not isinstance(node, dict):
                continue
            evidence_id = str(node.get("evidence_id") or "")
            source = evidence_by_id.get(evidence_id)
            if (
                source is None
                or evidence_id not in main_evidence_ids
                or evidence_id not in rendered_bindings.get(claim.local_id, set())
                or node.get("node_type") not in {"original", "repost", "response", "independent"}
            ):
                continue
            if institution_markers and not any(
                marker
                in f"{source.title or ''} {(source.content_text or source.snippet or '')[:2500]}"
                for marker in institution_markers
            ):
                continue
            nodes[evidence_id] = {
                "evidence_id": evidence_id,
                "publisher": str(
                    node.get("publisher")
                    or source.publisher_entity
                    or source.source_name
                    or source.source_domain
                )[:120],
                "published_at": source.published_at,
                "node_type": node["node_type"],
                "framing": str(node.get("framing") or claim.text)[:300],
                "claim_ref": claim.local_id,
                "evidence_refs": [evidence_id],
            }
            for edge in data.get("propagation_edges", []):
                if not isinstance(edge, dict):
                    continue
                from_id = str(edge.get("from_evidence_id") or "")
                to_id = str(edge.get("to_evidence_id") or "")
                relation = str(edge.get("relation") or "")
                if (
                    from_id in main_evidence_ids
                    and to_id in main_evidence_ids
                    and from_id != to_id
                    and relation in {"repost", "response", "follow_up"}
                ):
                    edges.append(
                        {
                            "from_evidence_id": from_id,
                            "to_evidence_id": to_id,
                            "relation": relation,
                            "claim_ref": claim.local_id,
                        }
                    )
        unique_edges = list(
            {
                (item["from_evidence_id"], item["to_evidence_id"], item["relation"]): item
                for item in edges
                if item["from_evidence_id"] in nodes and item["to_evidence_id"] in nodes
            }.values()
        )
        sufficient = len(nodes) >= 2 and bool(unique_edges)
        if not sufficient:
            limitations["items"].append(
                {
                    "id": "L16",
                    "category": "传播分析证据不足",
                    "text": f"仅形成 {len(nodes)} 个合格发布节点和 {len(unique_edges)} 条可追溯关系；未把普通事件事实或搜索命中数包装成传播路径。",
                }
            )
        return (
            {
                "block_id": "b_04_publication_network",
                "type": "propagation_network",
                "section": "04",
                "in_brief": sufficient,
                "title": "媒体发布与回应关系",
                "data_basis": "typed_claim_evidence",
                "nodes": list(nodes.values()),
                "edges": unique_edges,
                "fallback_text": None
                if sufficient
                else "传播分析证据不足：至少需要两个可信发布节点和一条可追溯的转载、回应或跟进关系。",
            },
            len(nodes),
            len(unique_edges),
        )

    @staticmethod
    def _time_span_days(evidence: Sequence[Any]) -> int:
        parsed: list[datetime] = []
        for item in evidence:
            if not item.published_at:
                continue
            try:
                stamp = datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
                parsed.append(
                    stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
                )
            except ValueError:
                continue
        if len(parsed) < 2:
            return 1 if parsed else 0
        return max(1, (max(parsed) - min(parsed)).days)

    @staticmethod
    def _propagation_blocks(
        evidence: Sequence[Any],
        limitations: dict[str, Any],
        hot_points: Sequence[HotSnapshotPoint] = (),
    ) -> list[dict[str, Any]]:
        numeric_hot_points = [item for item in hot_points if item.heat_value is not None]
        if hot_points and not numeric_hot_points:
            limitations["items"].append(
                {
                    "id": "L15",
                    "category": "热榜热度",
                    "text": "热榜命中记录缺少可比较的数值热度，未绘制热度曲线。",
                }
            )
        if len(evidence) < 3:
            limitations["items"].append(
                {
                    "id": "L04",
                    "category": "传播分析",
                    "text": f"仅有 {len(evidence)} 条证据，传播图表已降级为文字，避免把小样本画成趋势。",
                }
            )
            blocks = [
                {
                    "block_id": "b_04_propagation_fallback",
                    "type": "text",
                    "section": "04",
                    "in_brief": False,
                    "title": "传播分析",
                    "fallback_text": f"当前仅收录 {len(evidence)} 条公开证据，样本不足，未生成传播图表。",
                    "limitation_ref": "L04",
                }
            ]
            if numeric_hot_points:
                blocks.append(FullReportBuilder._hot_chart(numeric_hot_points))
            return blocks

        def count(field: str, fallback: str) -> list[dict[str, Any]]:
            values = Counter(str(getattr(item, field, None) or fallback) for item in evidence)
            return [{"label": label, "value": value} for label, value in sorted(values.items())]

        dates = Counter(
            (item.published_at or "日期未知")[:10] if item.published_at else "日期未知"
            for item in evidence
        )
        blocks = [
            {
                "block_id": "b_04_time_chart",
                "type": "chart",
                "section": "04",
                "in_brief": False,
                "title": "报道数量时间分布",
                "data_basis": "evidence_database",
                "chart_kind": "bar",
                "items": [{"label": key, "value": value} for key, value in sorted(dates.items())],
            },
            {
                "block_id": "b_04_source_chart",
                "type": "chart",
                "section": "04",
                "in_brief": False,
                "title": "信源类型分布",
                "data_basis": "evidence_database",
                "chart_kind": "bar",
                "items": count("source_role", "unknown"),
            },
            {
                "block_id": "b_04_platform_chart",
                "type": "chart",
                "section": "04",
                "in_brief": False,
                "title": "来源主体分布",
                "data_basis": "evidence_database",
                "chart_kind": "bar",
                "items": count("publisher_entity", "unknown"),
            },
        ]
        if numeric_hot_points:
            blocks.append(FullReportBuilder._hot_chart(numeric_hot_points))
        return blocks

    @staticmethod
    def _hot_chart(hot_points: Sequence[HotSnapshotPoint]) -> dict[str, Any]:
        groups: dict[tuple[str, str], list[HotSnapshotPoint]] = defaultdict(list)
        for item in hot_points:
            if item.heat_value is not None and isfinite(item.heat_value) and item.heat_value >= 0:
                groups[(item.platform, item.url or item.title)].append(item)
        series = []
        for (platform, _topic), points in sorted(groups.items()):
            ordered = sorted(points, key=lambda item: item.captured_at)
            # 同一采集时点的重复数据不组成伪趋势；保留最高排名的记录。
            by_time = {}
            for point in sorted(ordered, key=lambda item: item.rank, reverse=True):
                by_time[point.captured_at] = point
            ordered = sorted(by_time.values(), key=lambda item: item.captured_at)
            series.append(
                {
                    "block_id": f"b_04_hot_series_{len(series) + 1}",
                    "type": "chart",
                    "section": "04",
                    "in_brief": False,
                    "title": f"{platform} · {ordered[0].title}",
                    "data_basis": "hot_snapshot_database",
                    "chart_kind": "line" if len(ordered) >= 2 else "bar",
                    "items": [
                        {
                            "label": item.captured_at,
                            "timestamp": item.captured_at,
                            "value": item.heat_value,
                            "rank": item.rank,
                            "title": item.title,
                        }
                        for item in ordered
                    ],
                    "note": "仅比较同平台、同话题的已采集热榜值，不等于阅读量；各序列独立刻度，不跨平台相加。"
                    + ("只有一个采集时点，不推断趋势。" if len(ordered) < 2 else ""),
                }
            )
        if len(series) == 1:
            return {**series[0], "block_id": "b_04_hot_chart"}
        return {
            "block_id": "b_04_hot_chart",
            "type": "chart",
            "section": "04",
            "in_brief": False,
            "title": "分平台、分话题的热榜记录",
            "data_basis": "hot_snapshot_database",
            "chart_kind": "series",
            "items": [],
            "series": series,
        }
