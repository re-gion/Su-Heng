from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

from yuqing.agents.runtime import InvestigationAgent, SearchQuery
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchProvider
from yuqing.core.search.base import SearchParams, SearchProvider
from yuqing.services.comment_plugin import (
    CommentCandidateInput,
    CommentPluginService,
    adapter_for_url,
)
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.forum import ForumBoard, ForumMessageCreate
from yuqing.services.full_report import FullReportBuilder, ReportSectionAgent
from yuqing.services.historical_data import HistoricalDataService
from yuqing.services.moderation import Moderator, ModeratorReview
from yuqing.services.verifier import ClaimVerifierService, EvidenceVerifier
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate
from yuqing.storage.snapshots import SnapshotStore


def _default_region(language: str) -> str:
    base = language.split("-", 1)[0].lower()
    return {
        "zh": "CN",
        "en": "US",
        "de": "DE",
        "fr": "FR",
        "es": "ES",
        "ja": "JP",
        "ko": "KR",
        "ru": "RU",
        "pt": "BR",
        "ar": "SA",
    }.get(base, base.upper())


def ensure_requested_languages(plan, requested_languages: Sequence[str], event_query: str):
    """LLM 负责建议检索词，代码负责保证用户明确选择的语言实际执行。"""

    normalized = list(dict.fromkeys(item.lower() for item in requested_languages))[:3]
    existing = {item.language.lower(): item for item in plan.queries}
    queries: list[SearchQuery] = []
    for language in normalized:
        query = existing.get(language)
        if query is None:
            suffix = "最新报道" if language.startswith("zh") else "latest reports"
            query = SearchQuery(
                query=f"{event_query[:160]} {suffix}"[:200],
                language=language,
                region=_default_region(language),
            )
        queries.append(query)
    queries.extend(item for item in plan.queries if item.language.lower() not in normalized)
    return plan.model_copy(update={"queries": queries[:6]})


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
        historical_data: HistoricalDataService | None = None,
        comment_plugin: CommentPluginService | None = None,
        comment_agent: InvestigationAgent | None = None,
        translator: object | None = None,
        comment_evaluator: object | None = None,
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
        self.historical_data = historical_data or HistoricalDataService(database)
        self.comment_plugin = comment_plugin
        self.comment_agent = comment_agent
        self.comment_evaluator = comment_evaluator
        self.reports = FullReportBuilder(
            database,
            reports_dir,
            entailment,
            reporter,
            historical_data=self.historical_data,
            translator=translator,
        )
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

    async def _prepare_comment_candidates(self, task, investigation_query: str) -> int:
        if self.comment_plugin is None or not self.comment_plugin.enabled:
            return 0
        if self.comment_evaluator is not None:
            self.comment_plugin.evaluator = self.comment_evaluator
        inputs: list[CommentCandidateInput] = []
        for item in await self.database.list_evidence(task.id):
            try:
                adapter_for_url(item.url)
            except ValueError:
                continue
            inputs.append(
                CommentCandidateInput(
                    url=item.url,
                    title=item.title,
                    snippet=item.snippet or "",
                    published_at=item.published_at,
                )
            )
        if task.comment_mode in {"smart", "hybrid"}:
            domains = {
                "weibo": ["weibo.com"],
                "bilibili": ["bilibili.com"],
                "zhihu": ["zhihu.com"],
                "xiaohongshu": ["xiaohongshu.com"],
                "douyin": ["douyin.com"],
                "kuaishou": ["kuaishou.com"],
                "tieba": ["tieba.baidu.com"],
            }
            for platform, include_domains in domains.items():
                if not await self._reserve_tool("search"):
                    break
                try:
                    results = await self.search.search(
                        SearchParams(
                            query=f"{task.event_query} {platform} 评论 热议",
                            top_k=3,
                            include_domains=include_domains,
                            lang="zh",
                            region="CN",
                        )
                    )
                except Exception as exc:
                    self._limitations.append(
                        f"{platform} 候选帖子搜索失败（{type(exc).__name__}），可手工补充 URL。"
                    )
                    continue
                for result in results:
                    try:
                        adapter_for_url(result.url)
                    except ValueError:
                        continue
                    inputs.append(
                        CommentCandidateInput(
                            url=result.url,
                            title=result.title,
                            snippet=result.snippet,
                            published_at=result.published_at.isoformat()
                            if result.published_at
                            else None,
                        )
                    )
        candidates = await self.comment_plugin.discover_candidates(task, inputs)
        await self.events.emit(
            task.id,
            "agent.status",
            {
                "agent": "comment_insight",
                "phase": "awaiting_selection",
                "candidates": len(candidates),
            },
        )
        return len(candidates)

    async def _run_comment_insight(self, task_id: str, event_query: str, board: ForumBoard) -> None:
        if self.comment_agent is None:
            return
        comment_evidence = [
            item
            for item in await self.database.list_evidence(task_id)
            if item.kind == "social_comments"
        ]
        if not comment_evidence:
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": "comment_insight", "phase": "skipped", "inner_round": 0},
            )
            return
        await self.events.emit(
            task_id,
            "agent.status",
            {"agent": "comment_insight", "phase": "summarizing", "inner_round": 1},
        )
        generated = await self.comment_agent.summarize(event_query, comment_evidence)
        refs: list[str] = []
        findings: list[str] = []
        for item in generated:
            allowed = [
                ref for ref in item.evidence_ids if ref in {e.local_id for e in comment_evidence}
            ]
            if not allowed:
                continue
            # 评论洞察是样本观点，不进入事实 claim / 徽章 / 独立信源统计。
            refs.extend(allowed)
            findings.append(item.text)
        await self._post(
            board,
            ForumMessageCreate(
                task_id=task_id,
                round=1,
                agent="comment_insight",
                type="summary",
                content="；".join(findings) or "评论样本不足，未形成可引用观点。",
                refs=sorted(set(refs)),
                payload={"sampling_scope": "已确认帖子的脱敏评论样本，不代表整体民意"},
            ),
        )
        await self.events.emit(
            task_id, "agent.status", {"agent": "comment_insight", "phase": "done", "inner_round": 1}
        )

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
        if agent_name == "history_insight":
            try:
                task = await self.database.get_task(task_id)
                evidence_before = {
                    item.local_id for item in await self.database.list_evidence(task_id)
                }
                local_context = await self.historical_data.prepare_task_context(
                    task_id,
                    event_query,
                    date_from=task.time_range_from if task else None,
                    date_to=task.time_range_to if task else None,
                )
                if local_context:
                    scoped_query += f"\n{local_context}"
                    local_matches = await self.historical_data.task_matches(task_id)
                    await self.events.emit(
                        task_id,
                        "search.result",
                        {
                            "agent": agent_name,
                            "provider": "local_history",
                            "query": event_query,
                            "hits": len(local_matches),
                            "degraded_from": None,
                        },
                    )
                    for record in await self.database.list_evidence(task_id):
                        if record.local_id in evidence_before or record.provider != "local_dataset":
                            continue
                        await self.events.emit(
                            task_id,
                            "evidence.added",
                            {
                                "evidence_id": record.local_id,
                                "title": record.title,
                                "source_name": record.source_name or record.source_domain,
                                "source_tier": record.source_tier,
                                "published_at": record.published_at,
                                "agent": agent_name,
                                "provenance": "本地库命中",
                            },
                        )
            except Exception as exc:
                self._limitations.append(
                    f"本地历史层不可用（{type(exc).__name__}），历史 Agent 已独立降级到搜索回溯。"
                )
        await self.events.emit(
            task_id,
            "agent.status",
            {"agent": agent_name, "phase": "planning", "inner_round": 1},
        )
        plan = await agent.plan(scoped_query)
        task = await self.database.get_task(task_id)
        requested_languages = task.source_languages if task else ["zh", "en"]
        plan = ensure_requested_languages(plan, requested_languages, event_query)
        claims_before = {item.text for item in await self.database.list_claims(task_id)}
        findings: list[str] = []
        last_reason = "达到小 Loop 上限"
        for inner_round in range(1, self.max_inner_rounds + 1):
            query_limit = 1 if top_k <= 5 else (3 if top_k <= 8 else 4)
            queries = plan.queries[: max(query_limit, len(requested_languages))]
            for query_item in queries:
                query = (
                    query_item
                    if isinstance(query_item, SearchQuery)
                    else SearchQuery(query=str(query_item), language="zh", region="CN")
                )
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
                        "queries": [query.model_dump(mode="json")],
                    },
                )
                results = await self.search.search(
                    SearchParams(
                        query=query.query,
                        top_k=top_k,
                        freshness="oneYear" if agent_name == "history_insight" else "noLimit",
                        lang=query.language,
                        region=query.region,
                    )
                )
                records = await self.evidence.add_search_results(task_id, query.query, results)
                degraded_from = getattr(self.search, "last_degraded_from", None)
                provider_name = getattr(self.search, "last_provider", self.search.name)
                await self.events.emit(
                    task_id,
                    "search.result",
                    {
                        "agent": agent_name,
                        "provider": provider_name,
                        "query": query.query,
                        "language": query.language,
                        "hits": len(results),
                        "degraded_from": degraded_from,
                    },
                )
                for record in records:
                    updated = record
                    if record.fetch_status == "discovered":
                        if not await self._reserve_tool("fetch"):
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
            plan.queries = [
                SearchQuery(
                    query=value,
                    language=requested_languages[index % len(requested_languages)],
                    region=_default_region(requested_languages[index % len(requested_languages)]),
                )
                for index, value in enumerate(reflection.next_queries[:3])
            ]
            plan = ensure_requested_languages(plan, requested_languages, event_query)

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
            limitations = [review.reason] + [
                f"主持人评审降级时仍未解决：{item}" for item in review.unresolved_critical
            ]
            self._limitations.extend(item for item in limitations if item not in self._limitations)
        payload = review.model_dump(mode="json")
        if review.diagnostics:
            payload["diagnostics"] = review.diagnostics
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
        investigation_query += (
            f"\n信源范围：{task.source_scope}；检索语言：{', '.join(task.source_languages)}。"
            "最终报告使用中文；外文原文不可被译文替换。"
        )
        if not stop_requested:
            await self.database.set_task_status(task_id, "running", "forum")
            await self.events.emit(
                task_id,
                "task.status",
                {"status": "running", "phase": "forum", "progress": 5},
            )
        if phase not in {"investigated", "comment_selection", "comments_ready", "verified"}:
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
        phase = checkpoint.get("phase") if checkpoint else None
        if (
            not stop_requested
            and task.comment_mode != "off"
            and self.comment_plugin is not None
            and self.comment_plugin.enabled
            and phase == "investigated"
        ):
            candidates = await self._prepare_comment_candidates(task, investigation_query)
            await self.database.save_checkpoint(
                task_id,
                "comments:selection",
                {"phase": "comment_selection", "candidate_count": candidates},
            )
            await self.database.set_task_status(task_id, "paused", "comment_selection")
            await self.events.emit(
                task_id,
                "task.status",
                {"status": "paused", "phase": "comment_selection", "progress": 60},
            )
            return
        if phase == "comments_ready":
            await self._run_comment_insight(task_id, investigation_query, board)
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
