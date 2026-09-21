from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Protocol

from yuqing.render.html import render_html
from yuqing.render.validator import validate_report
from yuqing.services.forum import ForumMessage
from yuqing.services.historical_data import HistoricalDataService, HotSnapshotPoint
from yuqing.services.investigation_scope import ReportReleaseAssessment
from yuqing.services.report_analysis import (
    assemble_analysis,
    deduplicate_limitations,
    event_timeline,
    report_context,
)
from yuqing.services.report_builder import BriefReportBuilder, EntailmentVerifier
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
    ):
        self.database = database
        self.reports_dir = Path(reports_dir)
        self.brief = BriefReportBuilder(database, reports_dir, entailment_verifier)
        self.reporter = reporter
        self.historical_data = historical_data or HistoricalDataService(database)
        self.translator = translator

    async def build(
        self,
        task_id: str,
        *,
        forum: Sequence[ForumMessage] = (),
        orchestration_limitations: Sequence[str] = (),
        diagnostic_only: bool = False,
    ) -> tuple[str, dict[str, Any], str]:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        report_id, report, path = await self.brief.build(task_id)
        evidence = await self.database.list_evidence(task_id)
        claims = await self.database.list_claims(task_id)
        has_explicit_window = bool(task.time_range_from or task.time_range_to)
        main_evidence = [
            item
            for item in evidence
            if (item.extra or {}).get("scope_status") in {"main", "foreign_supplement"}
            or (not has_explicit_window and not (item.extra or {}).get("scope_status"))
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
                    "kind": item.kind,
                    "lang": item.lang or "unknown",
                    "original_excerpt": (item.content_text or item.snippet or "")[:1200],
                    "scope_status": (item.extra or {}).get("scope_status", "unclassified"),
                    "scope_label": (item.extra or {}).get("scope_label"),
                }
            )
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
            appendix.setdefault(
                "original_excerpt", (source.content_text or source.snippet or "")[:1200]
            )
            if (
                self.translator is not None
                and (source.lang or "zh").split("-", 1)[0].lower() != "zh"
                and appendix["original_excerpt"]
                and appendix.get("citations")
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
            main_evidence_total=len(main_evidence),
            background_evidence_total=len(evidence) - len(main_evidence),
        )
        header = by_type["report_header"]
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
        selected_dates = dated if len(dated) <= 12 else dated[:4] + dated[-8:]
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
            "note": "按被引用材料的发布日期排列，不等于事件发生日期或首发时间；仅显示有核验关联的材料，长时段展示首尾节点，不代表全网声量。",
        }

        total_claims = len(fact_items)
        verified_claims = sum(item["badge"] == "verified" for item in fact_items)
        unverified_claims = sum(item["badge"] == "unverified" for item in fact_items)
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
                    "value": f"{unverified_claims} / {total_claims}",
                    "note": unverified_note,
                    "tone": "warning" if unverified_claims else "neutral",
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
        publication_network, publication_nodes, propagation_edges = self._publication_network(
            claims, evidence_by_id, main_evidence_ids, by_type["limitations"]
        )
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
            """SELECT s.platform,c.status,c.collected_count,c.sampling_method,s.url,s.title
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
        history = {
            "block_id": "b_06_history",
            "type": "history_compare",
            "section": "06",
            "in_brief": False,
            "title": "历史对照",
            "cards": local_cards,
            "fallback_text": "未取得通过可比性审查的历史对照；同一事件的重复报道不作为比较案例。"
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

        enrichment: dict[str, Any] = {}
        if self.reporter is not None and not diagnostic_only:
            try:
                enrichment = await self.reporter.enrich(
                    report_context(report["task"], fact_items, main_evidence, forum)
                )
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
            enrichment, primary_fact_items, main_evidence
        )
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
                note="时点仅取自陈述正文明确写出的年月日，徽章保留该陈述的核验状态，不以网页发布日期替代事件日期。同日最多展示两个节点；完整陈述与其他来源可在核查记录展开。",
            )
        by_type["executive_summary"].update(summary)
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
            if block.get("type") == "analysis" and block.get("section") == "07"
            for item in block.get("items", [])
        )
        release = ReportReleaseAssessment.evaluate(
            concrete_event=bool(task.resolved_event_query or task.request_kind == "event"),
            main_evidence=len(main_evidence),
            verifiable_key_claims=definitive_claims,
            in_window_timeline_nodes=len(event_nodes),
            publication_nodes=publication_nodes,
            propagation_edges=propagation_edges,
            summary_has_what=bool(summary["what"]),
            summary_has_why=bool(summary["why"]),
            summary_has_action=bool(summary["so_what"]),
            evidence_bound_recommendations=recommendation_count,
        )
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
            block["section"] for block in analysis_blocks if block["type"] == "analysis"
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
            "text": f"调查主题：{task.resolved_event_query or task.event_query}。材料时间范围：{task.time_range_from or '未指定起点'} 至 {task.time_range_to or '未指定终点'}。"
            + (
                "已通过完整专报发布门；分析判断与已核验事实分开呈现，仍需关注各条不确定性。"
                if release.label == "full_report"
                else (
                    "当前仅输出检索诊断，没有足够的范围内主证据与可核验关键陈述。"
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
            *([history] if local_cards or "06" not in analysis_sections else []),
            *([] if "07" in analysis_sections else [recommendations]),
            by_type["limitations"],
            data_quality,
            by_type["evidence_appendix"],
        ]
        validated = validate_report(report).report
        await asyncio.to_thread(
            Path(path).write_text, render_html(validated, view="full"), encoding="utf-8"
        )
        return report_id, validated, path

    @staticmethod
    def _publication_network(
        claims: Sequence[Any],
        evidence_by_id: dict[str, Any],
        main_evidence_ids: set[str],
        limitations: dict[str, Any],
    ) -> tuple[dict[str, Any], int, int]:
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
                or evidence_id not in claim.evidence_ids
                or node.get("node_type") not in {"original", "repost", "response", "independent"}
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
