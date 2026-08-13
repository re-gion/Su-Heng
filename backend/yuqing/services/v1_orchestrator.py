from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

from yuqing.agents.runtime import InvestigationAgent
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchProvider
from yuqing.core.search.base import SearchParams, SearchProvider
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.forum import ForumBoard, ForumMessageCreate
from yuqing.services.full_report import FullReportBuilder, ReportSectionAgent
from yuqing.services.moderation import Moderator, ModeratorReview
from yuqing.services.verifier import ClaimVerifierService, EvidenceVerifier
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate
from yuqing.storage.snapshots import SnapshotStore


class V1Orchestrator:
    def __init__(
        self,
        database: Database,
        events: EventBus,
        *,
        search: SearchProvider,
        fetcher: FetchProvider,
        snapshots: SnapshotStore,
        agents: Mapping[str, InvestigationAgent],
        moderator: Moderator,
        verifier: EvidenceVerifier,
        reports_dir: Path,
        reporter: ReportSectionAgent | None = None,
        usage: object | None = None,
        models_used: Mapping[str, str] | None = None,
        closeables: Sequence[object] = (),
        max_outer_rounds: int = 2,
        max_inner_rounds: int = 3,
    ):
        self.database = database
        self.events = events
        self.search = search
        self.evidence = EvidenceStore(database, snapshots, fetcher)
        self.agents = dict(agents)
        self.moderator = moderator
        self.verification = ClaimVerifierService(database, verifier)
        entailment = verifier if hasattr(verifier, "entails") else None
        self.reports = FullReportBuilder(database, reports_dir, entailment, reporter)
        self.max_outer_rounds = max(1, max_outer_rounds)
        self.max_inner_rounds = max(1, max_inner_rounds)
        self.usage = usage
        self.models_used = dict(models_used or {})
        self.closeables = tuple(closeables)
        self._limitations: list[str] = []
        self._budget_lock = asyncio.Lock()
        self._search_calls = 0
        self._fetch_calls = 0
        self._budget_depth = "standard"

    async def aclose(self) -> None:
        for resource in self.closeables:
            close = getattr(resource, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    # 任务结果已落盘；资源关闭失败不应反向改写业务终态。
                    continue

    async def _emit_budget(self, task_id: str, depth: str) -> bool:
        tokens_used = int(getattr(self.usage, "tokens_used", 0))
        calls = int(getattr(self.usage, "calls", 0))
        token_limit = {"quick": 180000, "standard": 500000, "deep": 1000000}[depth]
        await self.database.update_task_usage(task_id, tokens_used=tokens_used, cost_estimate=0)
        await self.events.emit(
            task_id,
            "budget.update",
            {
                "tokens_used": tokens_used,
                "tokens_limit": token_limit,
                "calls": calls,
                "cost_estimate": 0,
            },
        )
        return tokens_used >= token_limit

    async def _reserve_tool(self, kind: str) -> bool:
        limits = {
            "quick": {"search": 10, "fetch": 8},
            "standard": {"search": 40, "fetch": 30},
            "deep": {"search": 80, "fetch": 60},
        }[self._budget_depth]
        async with self._budget_lock:
            used_name = "_search_calls" if kind == "search" else "_fetch_calls"
            used = int(getattr(self, used_name))
            if used >= limits[kind]:
                return False
            setattr(self, used_name, used + 1)
            return True

    async def _post(self, board: ForumBoard, value: ForumMessageCreate) -> None:
        message = await board.post(value)
        await self.events.emit(value.task_id, "forum.message", message.model_dump(mode="json"))

    async def _run_agent(
        self,
        *,
        task_id: str,
        event_query: str,
        agent_name: str,
        agent: InvestigationAgent,
        board: ForumBoard,
        outer_round: int,
        top_k: int,
    ) -> dict[str, object]:
        digest = board.digest_for(agent_name, outer_round - 1) if outer_round > 1 else ""
        scoped_query = event_query + (f"\n主持人/论坛补充：{digest}" if digest else "")
        await self.events.emit(
            task_id,
            "agent.status",
            {"agent": agent_name, "phase": "planning", "inner_round": 1},
        )
        plan = await agent.plan(scoped_query)
        claims_before = {item.text for item in await self.database.list_claims(task_id)}
        findings: list[str] = []
        last_reason = "达到小 Loop 上限"
        for inner_round in range(1, self.max_inner_rounds + 1):
            query_limit = 1 if top_k <= 5 else (3 if top_k <= 8 else 4)
            queries = plan.queries[:query_limit]
            for query in queries:
                if not await self._reserve_tool("search"):
                    self._limitations.append(
                        f"全局搜索调用已达 {self._search_calls} 次上限，停止新增检索。"
                    )
                    break
                await self.events.emit(
                    task_id,
                    "agent.status",
                    {
                        "agent": agent_name,
                        "phase": "searching",
                        "inner_round": inner_round,
                        "queries": [query],
                    },
                )
                results = await self.search.search(
                    SearchParams(
                        query=query,
                        top_k=top_k,
                        freshness="oneYear" if agent_name == "history_insight" else "noLimit",
                    )
                )
                records = await self.evidence.add_search_results(task_id, query, results)
                degraded_from = getattr(self.search, "last_degraded_from", None)
                provider_name = getattr(self.search, "last_provider", self.search.name)
                await self.events.emit(
                    task_id,
                    "search.result",
                    {
                        "agent": agent_name,
                        "provider": provider_name,
                        "query": query,
                        "hits": len(results),
                        "degraded_from": degraded_from,
                    },
                )
                for record in records:
                    if record.fetch_status != "fetched" and not await self._reserve_tool("fetch"):
                        self._limitations.append(
                            f"全局原文抓取已达 {self._fetch_calls} 次上限，剩余材料保留摘要。"
                        )
                        break
                    updated = await self.evidence.fetch_one(record)
                    await self.events.emit(
                        task_id,
                        "evidence.added",
                        {
                            "evidence_id": updated.local_id,
                            "title": updated.title,
                            "source_name": updated.source_name or updated.source_domain,
                            "source_tier": updated.source_tier,
                            "published_at": updated.published_at,
                            "agent": agent_name,
                        },
                    )
            inventory = await self.database.list_evidence(task_id)
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": agent_name, "phase": "summarizing", "inner_round": inner_round},
            )
            generated = await agent.summarize(scoped_query, inventory)
            new_claim_ids: list[str] = []
            for item in generated:
                if item.text in claims_before:
                    continue
                try:
                    claim = await self.database.add_claim(
                        ClaimCreate(
                            task_id=task_id,
                            text=item.text,
                            statement_kind=item.statement_kind,
                            rumor_text=item.rumor_text,
                            correction_text=item.correction_text,
                            agent=agent_name,
                            round=outer_round,
                            section={
                                "fact_investigator": "fact_check",
                                "media_propagation": "propagation",
                                "history_insight": "history",
                            }.get(agent_name, "fact_check"),
                            evidence_ids=item.evidence_ids,
                        )
                    )
                except ValueError as exc:
                    if "claim 总数已达" in str(exc):
                        await self.events.emit(
                            task_id,
                            "warning",
                            {
                                "code": "CLAIM_BUDGET_REACHED",
                                "message": str(exc),
                                "agent": agent_name,
                            },
                        )
                        break
                    raise
                claims_before.add(claim.text)
                new_claim_ids.append(claim.local_id)
                findings.append(claim.text)
                await self.events.emit(
                    task_id,
                    "claim.added",
                    {
                        "claim_id": claim.local_id,
                        "text": claim.text,
                        "evidence_ids": claim.evidence_ids,
                        "agent": agent_name,
                    },
                )
            claims = await self.database.list_claims(task_id)
            reflection = await agent.reflect(scoped_query, claims)
            last_reason = reflection.reason
            should_continue = bool(
                reflection.should_continue
                and reflection.next_queries
                and reflection.new_key_findings
                and inner_round < self.max_inner_rounds
            )
            await self.events.emit(
                task_id,
                "loop.round",
                {
                    "scope": "inner",
                    "agent": agent_name,
                    "round": inner_round,
                    "decision": "continue" if should_continue else "stop",
                    "reason": reflection.reason,
                },
            )
            if not should_continue:
                break
            plan.queries = reflection.next_queries[:3]

        refs = sorted(
            {
                ref
                for claim in await self.database.list_claims(task_id)
                if claim.agent == agent_name
                for ref in claim.evidence_ids
            }
        )
        summary = "；".join(findings[-8:]) or f"{agent_name} 本轮未形成可引用的新陈述。"
        finding_claims = [
            claim
            for claim in await self.database.list_claims(task_id)
            if claim.agent == agent_name and claim.text in findings[-8:]
        ]
        await self._post(
            board,
            ForumMessageCreate(
                task_id=task_id,
                round=outer_round,
                agent=agent_name,
                type="summary",
                content=summary,
                refs=refs,
                payload={
                    "stop_reason": last_reason,
                    "findings": [
                        {"claim_ref": claim.local_id, "evidence_refs": claim.evidence_ids}
                        for claim in finding_claims
                    ],
                },
            ),
        )
        await self.events.emit(
            task_id,
            "agent.status",
            {"agent": agent_name, "phase": "done", "inner_round": inner_round},
        )
        return {"agent": agent_name, "status": "success" if findings else "partial"}

    async def _run_outer_round(
        self,
        task_id: str,
        event_query: str,
        board: ForumBoard,
        outer_round: int,
        top_k: int,
        agent_names: Sequence[str],
    ) -> list[dict[str, object]]:
        tasks = {
            name: asyncio.create_task(
                self._run_agent(
                    task_id=task_id,
                    event_query=event_query,
                    agent_name=name,
                    agent=agent,
                    board=board,
                    outer_round=outer_round,
                    top_k=top_k,
                ),
                name=f"agent:{name}",
            )
            for name, agent in self.agents.items()
            if name in agent_names
        }
        results: list[dict[str, object]] = []
        for name, task in tasks.items():
            try:
                results.append(await task)
            except asyncio.CancelledError:
                results.append({"agent": name, "status": "cancelled"})
                self._limitations.append(f"{name} 被取消，相关调查可能不完整。")
            except Exception as exc:
                if "task investigation is sealed" in str(exc):
                    results.append({"agent": name, "status": "cancelled"})
                    await self.events.emit(
                        task_id,
                        "agent.status",
                        {"agent": name, "phase": "cancelled", "inner_round": 0},
                    )
                    continue
                results.append({"agent": name, "status": "failed"})
                text = (
                    f"{name} 运行失败（{type(exc).__name__}: {str(exc)[:180]}），其余 Agent 继续。"
                )
                self._limitations.append(text)
                await self.events.emit(
                    task_id,
                    "warning",
                    {"code": "AGENT_FAILED", "message": text, "agent": name},
                )
                await self.events.emit(
                    task_id, "agent.status", {"agent": name, "phase": "blocked", "inner_round": 0}
                )
        return results

    async def _moderate(
        self, task_id: str, event_query: str, board: ForumBoard, outer_round: int
    ) -> ModeratorReview:
        review = await self.moderator.review(
            event_query,
            board.history(round_number=outer_round),
            len(await self.database.list_evidence(task_id)),
            len(await self.database.list_claims(task_id)),
        )
        if review.degraded:
            self._limitations.append(review.reason)
            self._limitations.extend(
                f"主持人降级放行时仍未解决：{item}" for item in review.unresolved_critical
            )
        payload = review.model_dump(mode="json")
        await self.events.emit(task_id, "host.review", payload)
        await self._post(
            board,
            ForumMessageCreate(
                task_id=task_id,
                round=outer_round,
                agent="moderator",
                type="review",
                content=review.reason,
                payload=payload,
            ),
        )
        for directive in review.directives:
            await self._post(
                board,
                ForumMessageCreate(
                    task_id=task_id,
                    round=outer_round,
                    agent="moderator",
                    type="directive",
                    content=directive.instruction,
                    payload={"agent": directive.agent},
                ),
            )
        return review

    async def run_task(self, task_id: str) -> None:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        stop_requested = task.status == "stopping"
        self._budget_depth = task.depth
        token_limit = {"quick": 180000, "standard": 500000, "deep": 1000000}[task.depth]
        if self.usage is not None and hasattr(self.usage, "token_limit"):
            self.usage.token_limit = token_limit
        checkpoint = await self.database.latest_checkpoint(task_id)
        if self.models_used and not task.config_snapshot:
            await self.database.set_task_config_snapshot(
                task_id,
                {
                    "models_used": self.models_used,
                    "search_providers": [
                        provider.name for provider in getattr(self.search, "providers", [])
                    ],
                },
            )
        phase = checkpoint.get("phase") if checkpoint else None
        board = await ForumBoard.restore(self.database, task_id)
        if stop_requested:
            self._limitations.append(
                "用户从已暂停状态停止任务；系统封存现有证据，不再启动新一轮调查。"
            )
            await self.database.save_checkpoint(task_id, "outer:stopped", {"phase": "investigated"})
            phase = "investigated"
        investigation_query = task.event_query
        if task.time_range_from or task.time_range_to:
            investigation_query += (
                f"\n调查时间范围：{task.time_range_from or '不限'} 至 "
                f"{task.time_range_to or '不限'}。"
            )
        if task.user_note:
            investigation_query += f"\n用户补充说明：{task.user_note}"
        if not stop_requested:
            await self.database.set_task_status(task_id, "running", "forum")
            await self.events.emit(
                task_id,
                "task.status",
                {"status": "running", "phase": "forum", "progress": 5},
            )
        if phase not in {"investigated", "verified"}:
            start_round = int(checkpoint.get("next_outer_round", 1)) if checkpoint else 1
            if checkpoint is None:
                await self.database.save_checkpoint(
                    task_id, "v1:planned", {"phase": "outer", "next_outer_round": 1}
                )
            effective_outer_rounds = min(
                self.max_outer_rounds, {"quick": 1, "standard": 2, "deep": 3}[task.depth]
            )
            active_agents = ("fact_investigator",) if task.depth == "quick" else tuple(self.agents)
            for name in self.agents:
                if name not in active_agents:
                    await self.events.emit(
                        task_id,
                        "agent.status",
                        {"agent": name, "phase": "skipped", "inner_round": 0},
                    )
            for outer_round in range(start_round, effective_outer_rounds + 1):
                await self.database.set_outer_round(task_id, outer_round)
                await self.events.emit(
                    task_id,
                    "loop.round",
                    {
                        "scope": "outer",
                        "round": outer_round,
                        "decision": "start",
                        "reason": "三 Agent 并行调查",
                    },
                )
                await self._run_outer_round(
                    task_id,
                    investigation_query,
                    board,
                    outer_round,
                    {"quick": 5, "standard": 8, "deep": 10}[task.depth],
                    active_agents,
                )
                budget_exhausted = await self._emit_budget(task_id, task.depth)
                control_task = await self.database.get_task(task_id)
                if control_task and control_task.status == "pausing":
                    await self.database.save_checkpoint(
                        task_id,
                        f"outer{outer_round}:paused",
                        {"phase": "outer", "next_outer_round": outer_round},
                    )
                    await self.database.set_task_status(task_id, "paused", "forum")
                    await self.events.emit(
                        task_id,
                        "task.status",
                        {"status": "paused", "phase": "forum", "progress": 30},
                    )
                    return
                if control_task and control_task.status == "stopping":
                    self._limitations.append(
                        "用户提前停止调查，报告仅基于停止前已封存的证据与陈述。"
                    )
                    await self.database.save_checkpoint(
                        task_id, "outer:stopped", {"phase": "investigated"}
                    )
                    break
                review = await self._moderate(task_id, investigation_query, board, outer_round)
                high_gaps = [gap.desc for gap in review.gaps if gap.priority == "high"]
                release = review.release and not high_gaps
                forced = outer_round >= effective_outer_rounds or budget_exhausted
                if release or forced:
                    if forced and not release:
                        unresolved = review.unresolved_critical + high_gaps
                        self._limitations.extend(
                            [f"主持人强制放行时仍未解决：{item}" for item in unresolved]
                            or ["达到最大协作轮次后强制放行。"]
                        )
                    if budget_exhausted:
                        self._limitations.append(
                            "全局 token 预算已耗尽，主持人强制放行并基于现有证据出报告。"
                        )
                    await self.events.emit(
                        task_id,
                        "loop.round",
                        {
                            "scope": "outer",
                            "round": outer_round,
                            "decision": "release" if release else "force_release",
                            "reason": review.reason,
                        },
                    )
                    break
                await self.database.save_checkpoint(
                    task_id,
                    f"outer{outer_round}:continue",
                    {"phase": "outer", "next_outer_round": outer_round + 1},
                )
            await self.database.save_checkpoint(
                task_id, "outer:investigated", {"phase": "investigated"}
            )

        checkpoint = await self.database.latest_checkpoint(task_id)
        if checkpoint is None or checkpoint.get("phase") != "verified":
            await self.database.set_task_status(task_id, "running", "verifying")
            verify_limit = {"quick": 30, "standard": 100, "deep": 240}[task.depth]
            verify_used = 0
            claims = sorted(
                await self.database.list_claims(task_id),
                key=lambda item: (
                    not item.is_key,
                    {
                        "fact_check": 0,
                        "propagation": 1,
                        "history": 2,
                    }.get(item.section or "", 3),
                    item.local_id,
                ),
            )
            verification_exhausted = False
            for claim in claims:
                relation_count = len(await self.database.claim_evidence_rows(claim.pk))
                if verification_exhausted or verify_used + relation_count > verify_limit:
                    verification_exhausted = True
                    await self.database.set_claim_verification(
                        claim.pk,
                        badge="unverified",
                        verdict="not_mentioned",
                        reason="核验预算不足；以 claim 为原子整条跳过。",
                        state="skipped",
                        independent_sources=0,
                        max_source_tier=None,
                        verifier_model="budget_guard",
                    )
                    continue
                verify_used += relation_count
                verified = await self.verification.verify_claim(claim)
                await self.events.emit(
                    task_id,
                    "verify.progress",
                    {
                        "claim_id": verified.local_id,
                        "badge": verified.badge,
                        "verification_state": verified.verification_state,
                    },
                )
            if verification_exhausted:
                self._limitations.append(
                    "核验关系预算已用尽；从首条超预算 claim 起，其后 claim 均整条标为跳过。"
                )
            await self.database.save_checkpoint(task_id, "verify:complete", {"phase": "verified"})

        await self.database.set_task_status(task_id, "running", "reporting")
        report_id, report, html_path = await self.reports.build(
            task_id, forum=board.history(), orchestration_limitations=self._limitations
        )
        # 报告构建包含摘要蕴含与 reporter 调用，终态前再落一次最终 usage。
        await self._emit_budget(task_id, task.depth)
        await self.database.save_report(task_id, report_id, report, html_path, report["metrics"])
        for block in report["blocks"]:
            await self.events.emit(
                task_id,
                "report.section",
                {
                    "section_id": block["section"],
                    "title": block.get("title") or block.get("type"),
                    "ir_chunk": block,
                },
            )
        await self.events.emit(
            task_id,
            "report.done",
            {
                "report_id": report_id,
                "html_url": f"/api/reports/{report_id}/html?view=full",
                "metrics": report["metrics"],
            },
        )
        await self.events.emit_task_status(task_id, status="done", phase="finished", progress=100)

    async def resume_task(self, task_id: str) -> None:
        if await self.database.latest_checkpoint(task_id) is None:
            raise ValueError("没有可用检查点")
        await self.run_task(task_id)
