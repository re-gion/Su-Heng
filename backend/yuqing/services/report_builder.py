from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Protocol

from yuqing.render.html import render_html
from yuqing.render.validator import validate_report
from yuqing.storage.db import Database


class EntailmentVerifier(Protocol):
    async def entails(self, claim_text: str, summary_text: str) -> bool: ...


class BriefReportBuilder:
    def __init__(
        self,
        database: Database,
        reports_dir: Path,
        entailment_verifier: EntailmentVerifier | None = None,
    ):
        self.database = database
        self.reports_dir = Path(reports_dir)
        self.entailment_verifier = entailment_verifier

    async def build(self, task_id: str) -> tuple[str, dict, str]:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        claims = await self.database.list_claims(task_id)
        rendered_claims = []
        appendix: dict[str, dict] = {}
        for claim in claims:
            rows = await self.database.claim_evidence_rows(claim.pk)
            citations = []
            states = []
            for row in rows:
                states.append(row["fetch_status"])
                note = None
                if row["fetch_status"] == "discovered":
                    note = "原文未取得"
                elif row["fetch_status"] == "fetch_failed":
                    note = "原文抓取失败"
                citations.append(
                    {
                        "evidence_ref": row["evidence_id"],
                        "quote_type": row["quote_type"],
                        "quote": row["quote"],
                        "quote_start": row["quote_start"],
                        "quote_end": row["quote_end"],
                        "relation": row["relation"],
                        "note": note,
                    }
                )
                appendix.setdefault(
                    row["evidence_id"],
                    {
                        "evidence_ref": row["evidence_id"],
                        "title": row["title"],
                        "source_name": row["source_name"] or row["publisher_entity"],
                        "publisher_entity": row["publisher_entity"],
                        "source_tier": row["source_tier"],
                        "source_role": row["source_role"],
                        "published_at": row["published_at"],
                        "url": row["url"],
                        "citations": [],
                        "fetch_status": row["fetch_status"],
                        "snapshot_pk": row["evidence_pk"]
                        if row["fetch_status"] == "fetched"
                        else None,
                        "content_sha256": row["content_sha256"]
                        if "content_sha256" in row.keys()
                        else None,
                    },
                )
                appendix[row["evidence_id"]]["citations"].append(
                    {
                        "claim_ref": claim.local_id,
                        "quote": row["quote"],
                        "relation": row["relation"],
                        "note": note,
                    }
                )
            if not citations:
                continue
            grade = (
                "fulltext"
                if all(state == "fetched" for state in states)
                else ("snippet_only" if all(state != "fetched" for state in states) else "mixed")
            )
            rendered_claims.append(
                {
                    "claim_ref": claim.local_id,
                    "statement_kind": claim.statement_kind,
                    "text": claim.text,
                    "rumor_text": claim.rumor_text,
                    "correction_text": claim.correction_text,
                    "badge": claim.badge or "unverified",
                    "verdict": claim.verdict or "not_mentioned",
                    "verification_state": claim.verification_state,
                    "independent_sources": claim.independent_sources,
                    "max_source_tier": claim.max_source_tier,
                    "evidence_grade": grade,
                    "citations": citations,
                }
            )

        total = len(rendered_claims)
        verified = sum(item["badge"] == "verified" for item in rendered_claims)
        disputed = sum(item["badge"] == "disputed" for item in rendered_claims)
        refuted = sum(item["badge"] == "refuted" for item in rendered_claims)
        evidence_items = [appendix[key] for key in sorted(appendix)]
        metrics = {
            "key_claims_candidate": len(claims),
            "key_claims_rendered": total,
            "key_claims_rejected": len(claims) - total,
            "rejection_reasons": {},
            "key_claims_verification_skipped": sum(
                item["verification_state"] == "skipped" for item in rendered_claims
            ),
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
            "independent_publishers": len({item["publisher_entity"] for item in evidence_items}),
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
        summary_items = []
        rejected_summary = 0
        for item in rendered_claims[:12]:
            summary = {"text": item["text"], "claim_ref": item["claim_ref"]}
            if self.entailment_verifier is not None:
                try:
                    supported = await self.entailment_verifier.entails(
                        item["text"], summary["text"]
                    )
                except Exception:
                    supported = False
                if not supported:
                    rejected_summary += 1
                    continue
            summary_items.append(summary)
        if rejected_summary:
            limitations.append(
                {
                    "id": "L02",
                    "category": "摘要语义校验",
                    "text": f"{rejected_summary} 条摘要句未通过蕴含校验，已从执行摘要移除。",
                }
            )
        report = {
            "schema_version": "0.1",
            "min_reader_minor": 1,
            "report_id": report_id,
            "task": {
                "task_id": task.id,
                "event_query": task.event_query,
                "depth": task.depth,
                "generated_at": stamp,
            },
            "metrics": metrics,
            "blocks": [
                {
                    "block_id": "b_00_header",
                    "type": "report_header",
                    "section": "00",
                    "in_brief": True,
                    "event_title": task.event_query,
                    "subtitle": "单 Agent 公开证据核验速览",
                },
                {
                    "block_id": "b_01_summary",
                    "type": "executive_summary",
                    "section": "01",
                    "in_brief": True,
                    "is_editorial": False,
                    "lede": f"本报告围绕“{task.event_query}”检索公开材料并逐条核验。",
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
        validated = validate_report(report).report
        html = render_html(validated)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"{report_id}.html"
        path.write_text(html, encoding="utf-8")
        return report_id, validated, str(path)
