from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from yuqing.agents.runtime import InvestigationAgent, SearchQuery
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchProvider
from yuqing.core.llm.gateway import sanitize_upstream_message
from yuqing.core.search.base import SearchParams, SearchProvider
from yuqing.services.budget import DEFAULT_BUDGET_TABLE, DepthBudget
from yuqing.services.comment_plugin import (
    CommentCandidateInput,
    CommentPluginService,
    adapter_for_url,
)
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.forum import ForumBoard, ForumMessageCreate
from yuqing.services.full_report import FullReportBuilder, ReportSectionAgent
from yuqing.services.historical_data import HistoricalDataService
from yuqing.services.investigation_scope import (
    InvestigationScope,
    is_concrete_event_candidate,
    title_matches_subject,
)
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
    """LLM 负责建议检索词，代码把用户语言选择落实为硬允许列表。"""

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
    # 主调查不得把模型额外建议的语种当成用户授权。境外补充由独立阶段显式放行，
    # 不在这里偷偷扩大检索范围。
    return plan.model_copy(update={"queries": queries[:6]})


def valid_media_analysis(data: object, evidence_by_id: Mapping[str, object]) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("publication_node"), dict):
        return False
    node = data["publication_node"]
    evidence_id = str(node.get("evidence_id") or "")
    if (
        evidence_id not in evidence_by_id
        or node.get("node_type") not in {"original", "repost", "response", "independent"}
        or not str(node.get("publisher") or "").strip()
        or not str(node.get("framing") or "").strip()
    ):
        return False
    allowed_relations = {"repost", "response", "follow_up"}
    for edge in data.get("propagation_edges", []):
        if not isinstance(edge, dict):
            return False
        if (
            str(edge.get("from_evidence_id") or "") not in evidence_by_id
            or str(edge.get("to_evidence_id") or "") not in evidence_by_id
            or edge.get("relation") not in allowed_relations
        ):
            return False
    return True


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
        budgets: Mapping[str, DepthBudget] | None = None,
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
        self.budgets = dict(budgets or DEFAULT_BUDGET_TABLE)
        self.usage = usage
        self.models_used = dict(models_used or {})
        self.closeables = tuple(closeables)
        self._limitations: list[str] = []
        self._budget_lock = asyncio.Lock()
        self._search_calls = 0
        self._fetch_calls = 0
        self._budget_depth = "standard"

    def budget_for(self, depth: str) -> DepthBudget:
        return self.budgets.get(depth, DEFAULT_BUDGET_TABLE["standard"])

    async def _prepare_topic_candidates(self, task) -> list[dict[str, object]]:
        """Turn a broad topic into user-selectable concrete event candidates."""

        await self.events.emit_task_status(
            task.id, status="running", phase="topic_discovery", progress=3
        )
        scope = InvestigationScope(
            event_query=task.event_query,
            languages=tuple(task.source_languages),
            source_scope=task.source_scope,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        candidates: list[dict[str, object]] = []
        seen: list[set[str]] = []
        search_specs: list[tuple[str, str]] = []
        for language in task.source_languages:
            suffix = (
                " 具体事件 通报 回应"
                if language.startswith("zh")
                else " specific incident response"
            )
            search_specs.append((language, f"{task.event_query[:150]}{suffix}"[:200]))
        propose_queries = getattr(self.comment_evaluator, "propose_topic_queries", None)
        if propose_queries is not None:
            for language in task.source_languages:
                try:
                    proposed = await propose_queries(
                        task.event_query,
                        date_from=task.time_range_from,
                        date_to=task.time_range_to,
                        language=language,
                        limit=3,
                    )
                except Exception as exc:
                    self._limitations.append(
                        f"具体事件检索词扩展失败（{type(exc).__name__}），已使用确定性检索词。"
                    )
                    continue
                search_specs.extend((language, item) for item in proposed)
        deduped_specs = list(dict.fromkeys(search_specs))[:8]
        for language, query in deduped_specs:
            if not await self._reserve_tool("search"):
                break
            results = await self.search.search(
                SearchParams(
                    query=query,
                    top_k=10,
                    freshness="noLimit",
                    lang=language,
                    region=_default_region(language),
                )
            )
            accepted = 0
            rejected_reasons: dict[str, int] = {}
            for item in results:
                decision = scope.classify_result(item, agent="fact_investigator")
                if not decision.accepted or decision.bucket == "background":
                    for reason in decision.reasons:
                        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                    continue
                title = re.sub(r"\s+", " ", item.title).strip()
                title = re.split(r"\s+[|_-]\s+", title, maxsplit=1)[0].strip()
                if not is_concrete_event_candidate(title):
                    rejected_reasons["generic_page"] = rejected_reasons.get("generic_page", 0) + 1
                    continue
                if not title_matches_subject(task.event_query, title):
                    rejected_reasons["subject_not_in_title"] = (
                        rejected_reasons.get("subject_not_in_title", 0) + 1
                    )
                    continue
                terms = {
                    title[index : index + 2]
                    for index in range(max(0, len(title) - 1))
                    if not title[index : index + 2].isspace()
                }
                if any(
                    len(terms & previous) / max(1, len(terms | previous)) >= 0.55
                    for previous in seen
                ):
                    continue
                seen.append(terms)
                candidate_id = "tc_" + hashlib.sha256(item.url.encode()).hexdigest()[:12]
                candidates.append(
                    {
                        "id": candidate_id,
                        "title": title[:160],
                        "query": title[:180],
                        "source_name": item.source_name or "公开网页",
                        "url": item.url,
                        "published_at": item.published_at.isoformat()
                        if item.published_at
                        else None,
                        "date_status": (
                            "发布日期待原文复核"
                            if decision.bucket == "pending"
                            else "范围内"
                            if decision.bucket == "main"
                            else decision.bucket
                        ),
                    }
                )
                accepted += 1
                if len(candidates) >= 5:
                    break
            await self.events.emit(
                task.id,
                "search.result",
                {
                    "agent": "topic_discovery",
                    "provider": getattr(self.search, "last_provider", self.search.name),
                    "query": query,
                    "language": language,
                    "hits": accepted,
                    "raw_hits": len(results),
                    "rejected": rejected_reasons,
                },
            )
            if len(candidates) >= 5:
                break
        payload = {
            "phase": "topic_selection",
            "original_query": task.event_query,
            "candidates": candidates[:5],
            "manual_entry_allowed": True,
        }
        await self.database.save_checkpoint(task.id, "topic:selection", payload)
        await self.events.emit_task_status(
            task.id, status="paused", phase="topic_selection", progress=5
        )
        return candidates[:5]

    async def _prepare_comment_candidates(
        self, task, investigation_query: str
    ) -> dict[str, object]:
        if self.comment_plugin is None or not self.comment_plugin.enabled:
            return {"candidate_count": 0, "discovery_attempts": []}
        if self.comment_evaluator is not None:
            self.comment_plugin.evaluator = self.comment_evaluator
        evidence = await self.database.list_evidence(task.id)
        claims = await self.database.list_claims(task.id)
        context = [claim.text for claim in claims if claim.section != "history"][:8]
        context.extend(item.title for item in evidence[:8])
        candidate_query = task.event_query
        refine_query = getattr(self.comment_evaluator, "refine_query", None)
        if refine_query is not None:
            try:
                candidate_query = await refine_query(task.event_query, context)
            except Exception as exc:
                self._limitations.append(
                    f"评论候选检索词收敛失败（{type(exc).__name__}），已回退到用户主题。"
                )

        inputs: list[CommentCandidateInput] = []
        discovery_attempts: list[dict[str, object]] = []
        platform_inputs: set[str] = set()
        for item in evidence:
            try:
                adapter = adapter_for_url(item.url)
            except ValueError:
                continue
            platform_inputs.add(adapter.platform)
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
            missing_platforms = [
                platform for platform in domains if platform not in platform_inputs
            ]
            try:
                public_discovery = await self.comment_plugin.discover_public_candidates(
                    candidate_query, missing_platforms
                )
                public_inputs = public_discovery.items
                discovery_attempts.extend(
                    item.model_dump(mode="json") for item in public_discovery.attempts
                )
            except Exception as exc:
                public_inputs = []
                discovery_attempts.extend(
                    {
                        "platform": platform,
                        "status": "failed",
                        "count": 0,
                        "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    }
                    for platform in missing_platforms
                )
                self._limitations.append(
                    f"公开平台候选发现失败（{type(exc).__name__}），已回退到搜索 provider。"
                )
            for item in public_inputs:
                try:
                    adapter = adapter_for_url(item.url)
                except ValueError:
                    continue
                platform_inputs.add(adapter.platform)
                inputs.append(item)

            provider_rejected = 0
            for platform, include_domains in domains.items():
                if platform in platform_inputs:
                    continue
                if not await self._reserve_tool("search"):
                    break
                try:
                    results = await self.search.search(
                        SearchParams(
                            query=(
                                f"{candidate_query[:150]} {platform} 评论 site:{include_domains[0]}"
                            ),
                            top_k=3,
                            include_domains=include_domains,
                            lang="zh",
                            region="CN",
                        )
                    )
                except Exception as exc:
                    discovery_attempts.append(
                        {
                            "platform": platform,
                            "status": "failed",
                            "count": 0,
                            "error": f"provider: {type(exc).__name__}: {str(exc)[:160]}",
                        }
                    )
                    self._limitations.append(
                        f"{platform} 候选帖子搜索失败（{type(exc).__name__}），可手工补充 URL。"
                    )
                    continue
                accepted_for_platform = 0
                for result in results:
                    try:
                        adapter = adapter_for_url(result.url)
                    except ValueError:
                        provider_rejected += 1
                        continue
                    if adapter.platform != platform:
                        provider_rejected += 1
                        continue
                    platform_inputs.add(platform)
                    accepted_for_platform += 1
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
                discovery_attempts.append(
                    {
                        "platform": platform,
                        "status": "found" if accepted_for_platform else "empty",
                        "count": accepted_for_platform,
                        "error": None
                        if accepted_for_platform
                        else f"provider returned {len(results)} result(s), none matched the platform URL contract",
                    }
                )
        candidates = await self.comment_plugin.discover_candidates(
            task,
            inputs,
            relevance_query=candidate_query,
            minimum_smart_relevance=6,
        )
        await self.events.emit(
            task.id,
            "agent.status",
            {
                "agent": "comment_insight",
                "phase": "awaiting_selection",
                "candidates": len(candidates),
                "query": candidate_query,
                "platforms": sorted({item.platform for item in candidates}),
                "rejected_provider_results": provider_rejected
                if task.comment_mode in {"smart", "hybrid"}
                else 0,
            },
        )
        return {
            "candidate_count": len(candidates),
            "discovery_attempts": discovery_attempts,
            "query": candidate_query,
        }

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
        token_limit = self.budget_for(depth).token_limit
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
        budget = self.budget_for(self._budget_depth)
        limit = budget.search_calls if kind == "search" else budget.fetch_calls
        async with self._budget_lock:
            used_name = "_search_calls" if kind == "search" else "_fetch_calls"
            used = int(getattr(self, used_name))
            if used >= limit:
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
        search_phase: str = "primary",
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
        search_languages = list(requested_languages)
        if search_phase == "foreign_supplement" and "en" not in search_languages:
            search_languages.append("en")
        plan = ensure_requested_languages(plan, search_languages, event_query)
        scope = InvestigationScope(
            event_query=task.event_query if task else event_query,
            languages=tuple(requested_languages),
            source_scope=task.source_scope if task else "auto",
            date_from=task.time_range_from if task else None,
            date_to=task.time_range_to if task else None,
        )
        claims_before = {item.text for item in await self.database.list_claims(task_id)}
        findings: list[str] = []
        last_reason = "达到小 Loop 上限"
        for inner_round in range(1, self.max_inner_rounds + 1):
            query_limit = self.budget_for(self._budget_depth).queries_per_round
            queries = plan.queries[: max(query_limit, len(search_languages))]
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
                raw_results = await self.search.search(
                    SearchParams(
                        query=query.query,
                        top_k=top_k,
                        freshness="noLimit",
                        lang=query.language,
                        region=query.region,
                    )
                )
                results = []
                rejected_reasons: dict[str, int] = {}
                for result in raw_results:
                    decision = scope.classify_result(result, agent=agent_name, phase=search_phase)
                    if not decision.accepted:
                        for reason in decision.reasons:
                            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                        continue
                    results.append(
                        result.model_copy(
                            update={
                                "lang": decision.detected_language,
                                "raw": {**result.raw, "_scope": decision.as_extra()},
                            }
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
                        "raw_hits": len(raw_results),
                        "rejected": rejected_reasons,
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
                        updated = await self.evidence.fetch_one(
                            record, scope=scope, agent=agent_name, phase=search_phase
                        )
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
            complete_inventory = await self.database.list_evidence(task_id)
            if agent_name == "history_insight":
                inventory = [
                    item
                    for item in complete_inventory
                    if item.kind == "local_dataset"
                    or (item.extra or {}).get("scope_status") == "history"
                ]
            else:
                inventory = [
                    item
                    for item in complete_inventory
                    if (item.extra or {}).get("scope_status") in {"main", "foreign_supplement"}
                ]
            if not inventory:
                await self.events.emit(
                    task_id,
                    "agent.status",
                    {
                        "agent": agent_name,
                        "phase": "scope_empty",
                        "inner_round": inner_round,
                    },
                )
                last_reason = "没有通过主题、语言、来源与时间准入的材料"
                break
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": agent_name, "phase": "summarizing", "inner_round": inner_round},
            )
            existing_claims = await self.database.list_claims(task_id)
            if len(existing_claims) > 40:
                existing_claims = [*existing_claims[:20], *existing_claims[-20:]]
            existing_context = "\n".join(
                f"- {item.local_id}: {item.text}" for item in existing_claims
            )
            summarize_query = scoped_query
            if existing_context:
                summarize_query += (
                    "\n【已有陈述】以下内容只用于避免近义重复。若新材料带来新数字及口径、"
                    "新日期/回应时点、新主体、新矛盾或新的可核验事件维度，仍应新增陈述：\n"
                    + existing_context
                )
            generated = await agent.summarize(summarize_query, inventory)
            evidence_by_id = {item.local_id: item for item in inventory}
            agent_budget = self.budget_for(self._budget_depth)
            new_claim_ids: list[str] = []
            for item in generated:
                if item.text in claims_before:
                    continue
                if agent_name == "media_propagation" and not valid_media_analysis(
                    item.analysis_data, evidence_by_id
                ):
                    message = "媒体传播输出缺少合格发布节点或引用了未知传播关系，已退回，不写入普通事实陈述。"
                    if message not in self._limitations:
                        self._limitations.append(message)
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
                            analysis_data=item.analysis_data,
                        ),
                        max_claims=agent_budget.max_claims,
                        max_evidence_per_claim=agent_budget.max_evidence_per_claim,
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
                    language=search_languages[index % len(search_languages)],
                    region=_default_region(search_languages[index % len(search_languages)]),
                )
                for index, value in enumerate(reflection.next_queries[:3])
            ]
            plan = ensure_requested_languages(plan, search_languages, event_query)

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
        search_phase: str = "primary",
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
                    search_phase=search_phase,
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
                text = f"{name} 运行失败（{sanitize_upstream_message(exc)}），其余 Agent 继续。"
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
        token_limit = self.budget_for(task.depth).token_limit
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
        if (
            not stop_requested
            and task.request_kind == "topic_discovery"
            and not task.resolved_event_query
        ):
            if not checkpoint or checkpoint.get("phase") != "topic_selection":
                await self._prepare_topic_candidates(task)
            else:
                await self.database.set_task_status(task_id, "paused", "topic_selection")
            await self._emit_budget(task_id, task.depth)
            return
        phase = checkpoint.get("phase") if checkpoint else None
        board = await ForumBoard.restore(self.database, task_id)
        if stop_requested:
            self._limitations.append(
                "用户从已暂停状态停止任务；系统封存现有证据，不再启动新一轮调查。"
            )
            await self.database.save_checkpoint(task_id, "outer:stopped", {"phase": "investigated"})
            phase = "investigated"
        investigation_query = task.resolved_event_query or task.event_query
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
            depth_budget = self.budget_for(task.depth)
            effective_outer_rounds = min(self.max_outer_rounds, depth_budget.outer_rounds)
            active_agents = ("fact_investigator",) if task.depth == "quick" else tuple(self.agents)
            no_gain_rounds = 0
            search_phase = "primary"
            for name in self.agents:
                if name not in active_agents:
                    await self.events.emit(
                        task_id,
                        "agent.status",
                        {"agent": name, "phase": "skipped", "inner_round": 0},
                    )
            for outer_round in range(start_round, effective_outer_rounds + 1):
                before_main = sum(
                    (item.extra or {}).get("scope_status") in {"main", "foreign_supplement"}
                    for item in await self.database.list_evidence(task_id)
                )
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
                    depth_budget.top_k,
                    active_agents,
                    search_phase,
                )
                after_main = sum(
                    (item.extra or {}).get("scope_status") in {"main", "foreign_supplement"}
                    for item in await self.database.list_evidence(task_id)
                )
                no_gain_rounds = no_gain_rounds + 1 if after_main <= before_main else 0
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
                saturated = no_gain_rounds >= 2
                forced = outer_round >= effective_outer_rounds or budget_exhausted or saturated
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
                if task.source_scope == "domestic" and high_gaps:
                    search_phase = "foreign_supplement"
                    self._limitations.append(
                        "国内优先检索后仍有明确高优先级证据缺口，下一轮已启用带标签的境外补充检索；外文原文保留。"
                    )
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
            discovery = await self._prepare_comment_candidates(task, investigation_query)
            await self.database.save_checkpoint(
                task_id,
                "comments:selection",
                {"phase": "comment_selection", **discovery},
            )
            await self.events.emit_task_status(
                task_id, status="paused", phase="comment_selection", progress=60
            )
            if not discovery["candidate_count"]:
                await self.events.emit(
                    task_id,
                    "warning",
                    {
                        "code": "COMMENT_CANDIDATES_EMPTY",
                        "message": "系统候选为空，请补充帖子 URL 或明确跳过评论洞察。",
                    },
                )
            return
        if phase == "comments_ready":
            await self._run_comment_insight(task_id, investigation_query, board)
        if checkpoint is None or checkpoint.get("phase") != "verified":
            await self.events.emit_task_status(
                task_id, status="running", phase="verifying", progress=74
            )
            verify_limit = self.budget_for(task.depth).max_verify_calls
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
            incomplete_claims = 0
            scheduled_claims = []
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
                scheduled_claims.append(claim)

            semaphore = asyncio.Semaphore(4)

            async def verify_one(claim):
                async with semaphore:
                    return await self.verification.verify_claim(claim)

            for verified in await asyncio.gather(
                *(verify_one(claim) for claim in scheduled_claims)
            ):
                if verified.verification_state == "incomplete":
                    incomplete_claims += 1
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
            if incomplete_claims:
                self._limitations.append(
                    f"{incomplete_claims} 条陈述存在未能完成的核验关系（上游不可用或材料无法"
                    "回溯），已按契约强制标为待核验。这些条目是“没核完”，不是“没有依据”，"
                    "其未读到的材料中可能包含反证，请勿据此下结论。"
                )
            await self.database.save_checkpoint(task_id, "verify:complete", {"phase": "verified"})

        await self.events.emit_task_status(
            task_id, status="running", phase="reporting", progress=88
        )
        current_task = await self.database.get_task(task_id)
        evidence = await self.database.list_evidence(task_id)
        main_ids = {
            item.local_id
            for item in evidence
            if (item.extra or {}).get("scope_status") in {"main", "foreign_supplement"}
            or (
                current_task is not None
                and not (current_task.time_range_from or current_task.time_range_to)
                and not (item.extra or {}).get("scope_status")
            )
        }
        verified_claims = [
            claim
            for claim in await self.database.list_claims(task_id)
            if claim.verification_state == "complete"
            and claim.badge in {"verified", "disputed", "refuted"}
            and any(evidence_id in main_ids for evidence_id in claim.evidence_ids)
        ]
        diagnostic_only = not main_ids or not verified_claims
        if diagnostic_only:
            self._limitations.append(
                "未通过报告发布门：缺少范围内主证据或已完成核验的关键陈述；已停止昂贵的分章生成，仅输出检索诊断。"
            )
        await self.events.emit(
            task_id,
            "agent.status",
            {
                "agent": "reporter",
                "phase": "diagnostic" if diagnostic_only else "drafting_sections",
                "inner_round": 1,
            },
        )
        report_id, report, html_path = await self.reports.build(
            task_id,
            forum=board.history(),
            orchestration_limitations=self._limitations,
            diagnostic_only=diagnostic_only,
        )
        await self.events.emit(
            task_id,
            "agent.status",
            {"agent": "reporter", "phase": "rendered", "inner_round": 1},
        )
        # 报告构建包含分章综合分析与语义审查，终态前再落一次最终 usage。
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
