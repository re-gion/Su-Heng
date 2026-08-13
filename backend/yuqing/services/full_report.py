from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from yuqing.render.html import render_html
from yuqing.render.validator import validate_report
from yuqing.services.forum import ForumMessage
from yuqing.services.historical_data import HistoricalDataService, HotSnapshotPoint
from yuqing.services.report_builder import BriefReportBuilder, EntailmentVerifier
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
    ):
        self.database = database
        self.reports_dir = Path(reports_dir)
        self.brief = BriefReportBuilder(database, reports_dir, entailment_verifier)
        self.reporter = reporter
        self.historical_data = historical_data or HistoricalDataService(database)

    async def build(
        self,
        task_id: str,
        *,
        forum: Sequence[ForumMessage] = (),
        orchestration_limitations: Sequence[str] = (),
    ) -> tuple[str, dict[str, Any], str]:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        report_id, report, path = await self.brief.build(task_id)
        evidence = await self.database.list_evidence(task_id)
        claims = await self.database.list_claims(task_id)
        by_type = {block["type"]: block for block in report["blocks"]}
        appendix_items = by_type["evidence_appendix"]["items"]
        appendix_ids = {item["evidence_ref"] for item in appendix_items}
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
                }
            )
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
        )
        header = by_type["report_header"]
        header["subtitle"] = "三 Agent 协作 · 公开证据可核验专报"
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

        dated = sorted(
            (item for item in evidence if item.published_at),
            key=lambda item: item.published_at or "",
        )
        timeline = {
            "block_id": "b_02_timeline",
            "type": "timeline",
            "section": "02",
            "in_brief": False,
            "title": "事件时间线",
            "nodes": [
                {
                    "date": item.published_at,
                    "text": item.title,
                    "evidence_refs": [item.local_id],
                }
                for item in dated[:12]
            ],
            "fallback_text": "公开材料缺少可用发布日期，无法构建可靠时间线。"
            if not dated
            else None,
        }

        kpis = {
            "block_id": "b_00_kpi",
            "type": "kpi_grid",
            "section": "00",
            "in_brief": True,
            "data_basis": "evidence_database",
            "items": [
                {"label": "证据", "value": len(evidence)},
                {"label": "重要陈述", "value": len(claims)},
                {"label": "独立发布主体", "value": report["metrics"]["independent_publishers"]},
                {"label": "引用覆盖率", "value": f"{report['metrics']['citation_coverage']:.0%}"},
                {"label": "未加权核验通过率", "value": f"{report['metrics']['verified_rate']:.0%}"},
                {
                    "label": "信源加权通过率",
                    "value": f"{report['metrics']['weighted_verified_rate']:.0%}",
                },
                {
                    "label": "候选 / 拦截",
                    "value": f"{report['metrics']['key_claims_candidate']} / {report['metrics']['key_claims_rejected']}",
                },
            ],
        }
        hot_points = await self.historical_data.hotlist_query(
            task.event_query,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        propagation = self._propagation_blocks(evidence, by_type["limitations"], hot_points)
        summaries = [message for message in forum if message.type == "summary"]
        viewpoint = {
            "block_id": "b_05_viewpoints",
            "type": "viewpoint_list",
            "section": "05",
            "in_brief": False,
            "title": "情感与观点",
            "items": [
                {
                    "agent": message.agent,
                    "text": message.content,
                    "evidence_refs": [ref for ref in message.refs if ref.startswith("E")],
                }
                for message in summaries
            ],
            "fallback_text": "当前样本不足以形成可核验的观点分类，不输出情感百分比。"
            if not summaries
            else None,
        }
        history_messages = [message for message in summaries if message.agent == "history_insight"]
        local_history = await self.historical_data.task_matches(task_id)
        local_cards = [
            {
                "event_name": item.event_name,
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
        search_cards = [
            {
                "event_name": "搜索回溯发现",
                "comparison": message.content,
                "provenance": "搜索回溯",
                "evidence_refs": [ref for ref in message.refs if ref.startswith("E")],
            }
            for message in history_messages
            if any(ref.startswith("E") for ref in message.refs)
        ]
        history = {
            "block_id": "b_06_history",
            "type": "history_compare",
            "section": "06",
            "in_brief": False,
            "title": "历史对照",
            "cards": local_cards or search_cards,
            "fallback_text": "本轮未取得带来源的可靠历史对照，不以相似案例推演未来。"
            if not (local_cards or search_cards)
            else None,
        }
        recommendation_refs = [item.local_id for item in evidence[:3]]
        recommendations = {
            "block_id": "b_07_recommendations",
            "type": "recommendation",
            "section": "07",
            "in_brief": False,
            "title": "研判与建议",
            "is_editorial": True,
            "editorial_basis": "基于本报告已收录公开证据的风险沟通建议",
            "items": [
                {
                    "text": "优先回看证据卡中的原文关键句，再判断是否转发或采取行动。",
                    "evidence_refs": recommendation_refs,
                }
            ],
        }

        if self.reporter is not None:
            try:
                enrichment = await self.reporter.enrich(
                    {
                        "task": report["task"],
                        "metrics": report["metrics"],
                        "forum": [m.model_dump(mode="json") for m in forum],
                    }
                )
                report["reporter_notes"] = enrichment
                note = str(enrichment.get("organization_note") or "").strip()
                warnings = enrichment.get("section_warnings") or []
                if note:
                    recommendations["items"].append(
                        {
                            "text": note,
                            "evidence_refs": recommendation_refs,
                            "source": "综合报告 Agent",
                        }
                    )
                for warning in warnings if isinstance(warnings, list) else []:
                    if str(warning).strip():
                        by_type["limitations"]["items"].append(
                            {
                                "id": f"L9{len(by_type['limitations']['items'])}",
                                "category": "报告 Agent 提醒",
                                "text": str(warning).strip(),
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
        report["blocks"] = [
            header,
            kpis,
            by_type["executive_summary"],
            timeline,
            by_type["fact_check_table"],
            *propagation,
            viewpoint,
            history,
            recommendations,
            by_type["limitations"],
            by_type["evidence_appendix"],
        ]
        validated = validate_report(report).report
        await asyncio.to_thread(
            Path(path).write_text, render_html(validated, view="full"), encoding="utf-8"
        )
        return report_id, validated, path

    @staticmethod
    def _time_span_days(evidence: Sequence[Any]) -> int:
        parsed: list[datetime] = []
        for item in evidence:
            if not item.published_at:
                continue
            try:
                parsed.append(datetime.fromisoformat(item.published_at.replace("Z", "+00:00")))
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
        return {
            "block_id": "b_04_hot_chart",
            "type": "chart",
            "section": "04",
            "in_brief": False,
            "title": "真实热榜热度曲线",
            "data_basis": "hot_snapshot_database",
            "chart_kind": "line",
            "items": [
                {
                    "label": f"{item.captured_at[:16]} · {item.platform}",
                    "value": item.heat_value,
                    "rank": item.rank,
                    "title": item.title,
                }
                for item in hot_points
            ],
        }
