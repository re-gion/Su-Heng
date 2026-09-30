from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Protocol

from yuqing.render.html import render_html
from yuqing.render.ir_migrations import CURRENT_READER_MINOR, CURRENT_SCHEMA_VERSION
from yuqing.render.validator import prune_citation_backlinks, validate_report
from yuqing.services.institution_scope import (
    PROTECTED_SCOPES,
    InstitutionScopeReviewer,
    ScopeReview,
)
from yuqing.services.verification import is_attribution_claim, stance_drop_reason
from yuqing.storage.db import Database


class EntailmentVerifier(Protocol):
    async def entails(self, claim_text: str, summary_text: str) -> bool: ...


class BriefReportBuilder:
    def __init__(
        self,
        database: Database,
        reports_dir: Path,
        entailment_verifier: EntailmentVerifier | None = None,
        scope_reviewer: InstitutionScopeReviewer | None = None,
    ):
        self.database = database
        self.reports_dir = Path(reports_dir)
        self.entailment_verifier = entailment_verifier
        self.scope_reviewer = scope_reviewer

    async def build(self, task_id: str) -> tuple[str, dict, str]:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        claims = await self.database.list_claims(task_id)
        institution_scope = task.investigation_scope in PROTECTED_SCOPES
        event_title = task.resolved_event_query or task.event_query
        if institution_scope:
            if self.scope_reviewer:
                self.scope_reviewer.bind(self.database, task_id, task.investigation_scope)
                title_review = (await self.scope_reviewer.review([event_title], kind="title"))[0]
                event_title = title_review.text if title_review.allowed else "公开事件调查报告"
            else:
                event_title = "公开事件调查报告"
        scope_excluded = 0
        scope_pending = 0
        if institution_scope:
            if self.scope_reviewer is not None:
                self.scope_reviewer.bind(self.database, task_id, task.investigation_scope)
                decisions = await self.scope_reviewer.review(
                    [claim.text for claim in claims], kind="claim"
                )
            else:
                decisions = [
                    ScopeReview("incomplete", "reviewer_unavailable", c.text) for c in claims
                ]
            accepted = [
                d.allowed and d.text == c.text for c, d in zip(claims, decisions, strict=True)
            ]
            scope_excluded = sum(d.status == "rejected" for d in decisions)
            scope_pending = len(claims) - sum(accepted) - scope_excluded
            claims = [claim for claim, allowed in zip(claims, accepted, strict=True) if allowed]
        rendered_claims = []
        original_publishers: set[str] = set()
        appendix: dict[str, dict] = {}
        rejection_reasons: dict[str, int] = {}
        stance_drops: Counter[str] = Counter()
        rejected_citations = 0
        for claim in claims:
            rows = await self.database.claim_evidence_rows(claim.pk)
            citations = []
            states = []
            attribution = is_attribution_claim(claim.text)
            for row in rows:
                original_publishers.add(
                    row["publisher_entity"] or row["source_name"] or row["evidence_id"]
                )
                drop_reason = stance_drop_reason(
                    relation=row["relation"],
                    source_role=row["source_role"],
                    attribution_claim=attribution,
                )
                if drop_reason is not None:
                    stance_drops[drop_reason] += 1
                note = None
                if row["fetch_status"] == "discovered":
                    note = "原文未取得"
                elif row["fetch_status"] == "fetch_failed":
                    note = "原文抓取失败"
                valid_citation = not (
                    row["quote_type"] == "paraphrase" and row["relation"] in {None, "not_mentioned"}
                )
                if valid_citation:
                    states.append(row["fetch_status"])
                    citations.append(
                        {
                            "evidence_ref": row["evidence_id"],
                            "quote_type": row["quote_type"],
                            "quote": None if institution_scope else row["quote"],
                            "quote_start": None if institution_scope else row["quote_start"],
                            "quote_end": None if institution_scope else row["quote_end"],
                            "relation": row["relation"],
                            "note": note,
                        }
                    )
                    if institution_scope:
                        citations[-1]["quote_type"] = "redacted"
                else:
                    rejected_citations += 1
                appendix.setdefault(
                    row["evidence_id"],
                    {
                        "evidence_ref": row["evidence_id"],
                        "title": f"公开来源 {row['evidence_id']}"
                        if institution_scope
                        else row["title"],
                        "source_name": "公开来源"
                        if institution_scope
                        else row["source_name"] or row["publisher_entity"],
                        "publisher_entity": None if institution_scope else row["publisher_entity"],
                        "source_tier": row["source_tier"],
                        "source_role": row["source_role"],
                        "published_at": row["published_at"],
                        "url": row["url"],
                        "citations": [],
                        "fetch_status": row["fetch_status"],
                        "snapshot_pk": row["evidence_pk"]
                        if row["fetch_status"] == "fetched" and not institution_scope
                        else None,
                        "content_sha256": row["content_sha256"]
                        if "content_sha256" in row.keys()
                        else None,
                        "content_origin": (
                            json.loads(row["extra"] or "{}").get("content_origin")
                            if "extra" in row.keys()
                            else None
                        ),
                    },
                )
                appendix[row["evidence_id"]]["citations"].append(
                    {
                        "claim_ref": claim.local_id,
                        "quote": None if institution_scope else row["quote"],
                        "quote_redacted": institution_scope,
                        "relation": row["relation"],
                        "note": note,
                    }
                )
            if not citations:
                reason = "引用未通过回溯核验" if rows else "没有绑定证据"
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                continue
            grade = (
                "fulltext"
                if all(state == "fetched" for state in states)
                else ("snippet_only" if all(state != "fetched" for state in states) else "mixed")
            )
            rendered_claims.append(
                {
                    "claim_ref": claim.local_id,
                    "origin_agent": claim.agent,
                    "statement_kind": claim.statement_kind,
                    "text": claim.text,
                    "rumor_text": None if institution_scope else claim.rumor_text,
                    "correction_text": None if institution_scope else claim.correction_text,
                    "badge": claim.badge or "unverified",
                    "verdict": claim.verdict or "not_mentioned",
                    "verification_state": claim.verification_state,
                    "verify_reason": None if institution_scope else claim.verify_reason,
                    "independent_sources": claim.independent_sources,
                    "max_source_tier": claim.max_source_tier,
                    "evidence_grade": grade,
                    "citations": citations,
                }
            )

        if scope_excluded:
            rejection_reasons["范围审查明确拒绝"] = scope_excluded
        if scope_pending:
            rejection_reasons["范围审查尚未完成"] = scope_pending
        total = len(rendered_claims)
        verified = sum(item["badge"] == "verified" for item in rendered_claims)
        disputed = sum(item["badge"] == "disputed" for item in rendered_claims)
        refuted = sum(item["badge"] == "refuted" for item in rendered_claims)
        evidence_items = [appendix[key] for key in sorted(appendix)]
        metrics = {
            "key_claims_candidate": len(claims) + scope_excluded + scope_pending,
            "key_claims_rendered": total,
            "key_claims_rejected": len(claims) + scope_excluded + scope_pending - total,
            "institution_scope_excluded_claims": scope_excluded,
            "scope_review_incomplete_claims": scope_pending,
            "rejection_reasons": rejection_reasons,
            "key_claims_verification_skipped": sum(
                item["verification_state"] == "skipped" for item in rendered_claims
            ),
            "key_claims_verification_incomplete": sum(
                item["verification_state"] == "incomplete" for item in rendered_claims
            ),
            "stance_drops": dict(stance_drops),
            "citation_coverage": 1.0 if total else 0.0,
            "verified_rate": verified / total if total else 0.0,
            "weighted_verified_rate": (
                sum(
                    {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2}.get(item["max_source_tier"], 0.2)
                    for item in rendered_claims
                    if item["badge"] == "verified"
                )
                / sum(
                    {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2}.get(item["max_source_tier"], 0.2)
                    for item in rendered_claims
                )
                if total
                else 0.0
            ),
            "weight_scheme": "tier:1.0/0.8/0.6/0.4/0.2",
            "disputed_rate": disputed / total if total else 0.0,
            "refuted_rate": refuted / total if total else 0.0,
            "evidence_total": len(evidence_items),
            "evidence_fetched": sum(item["fetch_status"] == "fetched" for item in evidence_items),
            "evidence_snippet_only": sum(
                item["fetch_status"] != "fetched" for item in evidence_items
            ),
            # independent_publishers 与 time_span_days 是占位值：FullReportBuilder
            # 会在 metrics.update() 里用全量证据重算并覆盖（含 source_name/source_domain
            # 兜底），以保证报告里展示的独立信源数与计数口径一致。
            "independent_publishers": len(original_publishers),
            "time_span_days": 1,
        }
        report_id = uuid.uuid4().hex
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        limitations = [
            {
                "id": "L01",
                "category": "证据强度",
                "text": f"{metrics['evidence_snippet_only']} 条证据未取得原文或抓取失败，引用卡片已逐条标注。",
            }
        ]
        if rejected_citations:
            limitations.append(
                {
                    "id": "L02",
                    "category": "引用回溯",
                    "text": f"{rejected_citations} 条转述引用未通过原文回溯，已从事实正文移除。",
                }
            )
        if scope_excluded:
            limitations.append(
                {
                    "id": "L03",
                    "category": "机构范围",
                    "text": f"{scope_excluded} 条陈述经审查明确不符合调查范围或隐私边界，未进入报告。",
                }
            )
        if scope_pending:
            limitations.append(
                {
                    "id": "L04",
                    "category": "审查未完成",
                    "text": f"{scope_pending} 条陈述尚未完成范围审查或脱敏后核验，暂不展示；这不表示内容违规。",
                }
            )
        summary_items = []
        # 摘要逐字回填陈述，不再让模型判断同一句话是否蕴含自身。
        # 综合报告只选择编号；解释和建议进入独立编辑分析及语义审查。
        for item in rendered_claims[:6]:
            summary = {"text": item["text"], "claim_ref": item["claim_ref"]}
            summary_items.append(summary)
        report = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "min_reader_minor": CURRENT_READER_MINOR,
            "report_id": report_id,
            "task": {
                "task_id": task.id,
                "event_query": event_title,
                "investigation_scope": task.investigation_scope,
                "depth": task.depth,
                "source_scope": task.source_scope,
                "source_languages": task.source_languages,
                "comment_mode": task.comment_mode,
                "time_range_from": task.time_range_from,
                "time_range_to": task.time_range_to,
                "user_note": None if institution_scope else task.user_note,
                "generated_at": stamp,
            },
            "metrics": metrics,
            "blocks": [
                {
                    "block_id": "b_00_header",
                    "type": "report_header",
                    "section": "00",
                    "in_brief": True,
                    "event_title": event_title,
                    "subtitle": "单 Agent 公开证据核验速览",
                },
                {
                    "block_id": "b_01_summary",
                    "type": "executive_summary",
                    "section": "01",
                    "in_brief": True,
                    "is_editorial": False,
                    "lede": "本报告围绕机构公开回应与处理检索公开材料并逐条核验。"
                    if task.investigation_scope == "institution"
                    else f"本报告围绕“{event_title}”检索公开材料并逐条核验。",
                    "what": summary_items,
                    "why": [],
                    "so_what": [],
                },
                {
                    "block_id": "b_03_factcheck",
                    "type": "fact_check_table",
                    "section": "03",
                    "in_brief": True,
                    "items": rendered_claims,
                },
                {
                    "block_id": "b_08_limits",
                    "type": "limitations",
                    "section": "08",
                    "in_brief": True,
                    "items": limitations,
                },
                {
                    "block_id": "b_09_appendix",
                    "type": "evidence_appendix",
                    "section": "09",
                    "in_brief": False,
                    "items": evidence_items,
                },
            ],
        }
        prune_citation_backlinks(report)
        validated = validate_report(report).report
        html = render_html(validated)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"{report_id}.html"
        await asyncio.to_thread(path.write_text, html, encoding="utf-8")
        return report_id, validated, str(path)
