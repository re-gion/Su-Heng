"""Explicit comment deepening, preserving the published core and task budgets."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from yuqing.agents.comment_observations import fingerprint
from yuqing.core.comment_contract import reconcile_questions
from yuqing.core.llm.gateway import LLMBudgetExhausted, upstream_diagnostic
from yuqing.render.html import render_html
from yuqing.render.validator import validate_report
from yuqing.services.institution_scope import PROTECTED_SCOPES, SCOPE_POLICY_VERSION


async def deepen_comment_question(runner, task_id, question_id, *, follow_up=False):
    task = await runner.database.get_task(task_id)
    row = await runner.database.get_report_for_task(task_id)
    if not task or task.status != "done" or not row:
        raise ValueError("仅支持对已完成任务的已审评论问题深入分析")
    if await runner.database.report_under_review(row["id"]):
        raise ValueError("报告正在下线复核，不能开展深入分析")
    report = json.loads(row["ir_json"])
    block = next(
        (
            b
            for b in report["blocks"]
            if b["type"] == "comment_insight" and b.get("analysis_version") == 5
        ),
        None,
    )
    question = next((q for q in (block or {}).get("items", []) if q["id"] == question_id), None)
    if not question or question.get("review_status") != "accepted":
        raise ValueError("未找到对应的已审评论问题")
    if follow_up and (
        question.get("publicly_verifiable") is not True
        or not question.get("comparisons")
        or question["comparisons"][0]["status"] not in {"partial", "unanswered"}
    ):
        return {
            "status": "not_applicable",
            "message": "先对照已有材料，仅对有公开可核查缺口的问题开启补查。",
        }
    await runner._bind_task_runtime(task)
    review = None
    if task.investigation_scope in PROTECTED_SCOPES:
        if not runner.scope_reviewer:
            raise ValueError("隐私审查不可用，不能开展深入分析")

        async def review(texts):
            return await runner.scope_reviewer.review(texts, kind="report_text")

    public = await runner._comment_public_context(task_id)
    related = [o for o in block["observations"] if o["id"] in question["observation_refs"]]
    samples = block["samples"]
    key = f"comments:deep:{question_id}"
    material_fp = fingerprint(
        [
            SCOPE_POLICY_VERSION,
            runner.comment_agent.system_prompt,
            task.investigation_scope,
            {k: question.get(k) for k in ("id", "title", "summary", "observation_refs")},
            related,
            public,
            follow_up,
        ]
    )
    previous = await runner.database.checkpoint(task_id, key) or {}
    if previous.get("status") == "queued":
        previous = previous.get("cached_result", {})
    if previous.get("material_fingerprint") == material_fp and previous.get("status") == "complete":
        return {"status": "complete", "reused": True, "material_fingerprint": material_fp}
    if not runner._llm_phase_has_room(8000):
        return {
            "status": "budget_limited",
            "message": "原任务剩余模型额度不足，未发起深入分析；速读保留。",
        }
    if review:
        decisions = await runner.scope_reviewer.review(
            [s["text"] for s in samples if s["id"] in question["comment_refs"]], kind="comment"
        )
        if not all(d.allowed for d in decisions):
            return {
                "status": "scope_review_incomplete",
                "message": "关联样本未全部通过当前范围审查，未发起深入分析。",
            }
    checkpoint = {
        "status": "running",
        "material_fingerprint": material_fp,
        "mode": "follow_up" if follow_up else "existing",
    }
    await runner.database.save_checkpoint(task_id, key, checkpoint)

    async def save_review(review_key, decision):
        await runner.database.save_checkpoint(
            task_id, "comments:theme-review:" + review_key, decision
        )

    candidate = copy.deepcopy(report)
    target = next(b for b in candidate["blocks"] if b["type"] == "comment_insight")
    diagnostics = []
    outcome = None
    try:
        if follow_up:
            outcome = await runner._comment_follow_up(
                task_id, task.resolved_event_query or task.event_query, question, allow_done=True
            )
            target.setdefault("follow_ups", []).append(
                {
                    "question_ref": question_id,
                    "status": outcome["status"],
                    "evidence_refs": [e["evidence_ref"] for e in outcome.get("evidence", [])],
                }
            )
            public = list(
                {e["evidence_ref"]: e for e in [*public, *outcome.get("evidence", [])]}.values()
            )
        if not follow_up or outcome.get("evidence"):
            item, diagnostics = await runner.comment_agent.deepen_question(
                task.resolved_event_query or task.event_query,
                question,
                related,
                samples,
                public,
                review_observations=review,
                save_review_decision=save_review,
                run_key="",
            )
            # Keep the source-bound public ID stable for UI and citation links.
            if item:
                item["id"] = question_id
                for comp in item.get("comparisons", []):
                    old_id = comp["id"]
                    comp["id"] = question_id + "C"
                    for judgement in item.get("judgements", []):
                        judgement["comparison_refs"] = [
                            comp["id"] if r == old_id else r for r in judgement["comparison_refs"]
                        ]
                        judgement["id"] = question_id + "J"
                target["items"][
                    target["items"].index(
                        next(q for q in target["items"] if q["id"] == question_id)
                    )
                ] = item
            else:
                target["items"] = [q for q in target["items"] if q["id"] != question_id]
        target.setdefault("diagnostics", []).extend(diagnostics)
        if follow_up and outcome.get("evidence"):
            appendix = next(b for b in candidate["blocks"] if b["type"] == "evidence_appendix")
            known = {s["evidence_ref"] for s in appendix["items"]}
            sources = {e.local_id: e for e in await runner.database.list_evidence(task_id)}
            for source in outcome["evidence"]:
                ref = source["evidence_ref"]
                if ref in known:
                    continue
                e = sources[ref]
                labels = [e.title, e.source_name, e.snippet or ""]
                if review:
                    checked = await review(labels)
                    if not all(d.allowed for d in checked):
                        raise ValueError("补查来源展示字段未完成审查，未更新已发布报告")
                    labels = [d.text for d in checked]
                appendix["items"].append(
                    {
                        "evidence_ref": ref,
                        "title": labels[0],
                        "source_name": labels[1],
                        "snippet": labels[2],
                        "url": e.url,
                        "source_tier": e.source_tier,
                        "published_at": e.published_at,
                        "fetch_status": e.fetch_status,
                        "lang": e.lang,
                        "kind": e.kind,
                        "content_sha256": e.content_sha256,
                        "cited_in_report": True,
                    }
                )
        reconcile_questions(target)
        target.setdefault("stages", {})["evidence_comparison"] = (
            "partial" if any(q.get("comparisons") for q in target["items"]) else "incomplete"
        )
        # All new display text is privacy checked; validate the final optional module
        # again so stale policy decisions cannot enter a fresh delivery.
        if runner.scope_reviewer and task.investigation_scope in PROTECTED_SCOPES:
            await runner.reports._retain_reviewed_blocks(candidate, task)
            runner.reports._reconcile_reviewed_comment_themes(candidate)
        candidate = validate_report(candidate).report
        if await runner.database.report_under_review(row["id"]):
            raise ValueError("报告已进入复核，深入分析结果未发布")

        def publish(connection):
            changed = connection.execute(
                "UPDATE report SET ir_json=?,pdf_path=NULL WHERE id=? AND ir_json=?",
                (json.dumps(candidate, ensure_ascii=False), row["id"], row["ir_json"]),
            ).rowcount
            if changed != 1:
                raise ValueError("报告已被其他操作更新，未覆盖新的报告")

        await runner.database.write(publish)
        saved_analysis = await runner.database.checkpoint(task_id, "comments:analysis")
        if saved_analysis and isinstance(saved_analysis.get("analysis"), dict):
            analysis = copy.deepcopy(saved_analysis["analysis"])
            for field in (
                "items",
                "observations",
                "samples",
                "coverage",
                "priority_order",
                "follow_ups",
                "diagnostics",
            ):
                analysis[field] = copy.deepcopy(
                    target.get(field, [] if field != "coverage" else {})
                )
            analysis["public_context_fingerprint"] = fingerprint(
                await runner._comment_public_context(task_id)
            )
            await runner.database.save_checkpoint(
                task_id, "comments:analysis", {"phase": "comments_ready", "analysis": analysis}
            )
        destination = await asyncio.to_thread(Path(row["html_path"]).resolve)
        report_root = await asyncio.to_thread(runner.reports.reports_dir.resolve)
        if destination.is_relative_to(report_root):
            await asyncio.to_thread(
                destination.write_text, render_html(candidate), encoding="utf-8"
            )
        status = (
            outcome["status"]
            if follow_up and not outcome.get("evidence")
            else "complete"
            if any(q["id"] == question_id and q.get("comparisons") for q in target["items"])
            else "partial"
        )
        await runner.database.save_checkpoint(task_id, key, {**checkpoint, "status": status})
        await runner.events.emit(
            task_id, "comment.deepening", {"question_id": question_id, "status": status}
        )
        return {"status": status}
    except Exception as exc:
        status = "budget_limited" if isinstance(exc, LLMBudgetExhausted) else "incomplete"
        await runner.database.save_checkpoint(
            task_id,
            key,
            {
                **checkpoint,
                "status": status,
                "diagnostic": upstream_diagnostic(exc, stage="comment_deepening"),
            },
        )
        if status == "budget_limited":
            return {
                "status": status,
                "message": "剩余额度不足以完成本问题复核，已发布的速读与报告保留。",
            }
        raise
