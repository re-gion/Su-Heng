from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from openai import RateLimitError

from yuqing.agents.comment_observations import fingerprint
from yuqing.agents.runtime import GeneratedClaim, InvestigationAgent, Reflection, SearchQuery
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchProvider
from yuqing.core.llm.gateway import (
    LLMBudgetExhausted,
    rate_limit_diagnostic,
    sanitize_upstream_message,
    upstream_diagnostic,
)
from yuqing.core.search.base import SearchParams, SearchProvider, SearchResult
from yuqing.render.html import render_html
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
from yuqing.services.history_comparison import independent_case_evidence
from yuqing.services.institution_scope import (
    PROTECTED_SCOPES,
    PUBLIC_EVENT_INSTRUCTION,
    SCOPE_INSTRUCTION,
    SCOPE_POLICY_VERSION,
    InstitutionScopeReviewer,
)
from yuqing.services.investigation_scope import (
    InvestigationScope,
)
from yuqing.services.moderation import Moderator, ModeratorReview
from yuqing.services.source_tiers import bundled_classifier, registrable_domain
from yuqing.services.task_diagnostics import task_timing
from yuqing.services.topic_discovery import TopicDiscovery, TopicDiscoveryRequest
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
    allowed = [item for item in plan.queries if item.language.lower() in normalized]
    queries: list[SearchQuery] = []
    for language in normalized:
        selected = next((item for item in allowed if item.language.lower() == language), None)
        if selected is None:
            suffix = "最新报道" if language.startswith("zh") else "latest reports"
            selected = SearchQuery(
                query=f"{event_query[:160]} {suffix}"[:200],
                language=language,
                region=_default_region(language),
            )
        queries.append(selected)
    queries.extend(item for item in allowed if item not in queries)
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
        scope_reviewer: InstitutionScopeReviewer | None = None,
        comment_evaluator: object | None = None,
        topic_discovery: TopicDiscovery | None = None,
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
        self.scope_reviewer = scope_reviewer
        self.topic_discovery = topic_discovery or TopicDiscovery(search, fetcher)
        self.reports = FullReportBuilder(
            database,
            reports_dir,
            entailment,
            reporter,
            historical_data=self.historical_data,
            translator=translator,
            scope_reviewer=scope_reviewer,
        )
        self.max_outer_rounds = max(1, max_outer_rounds)
        self.max_inner_rounds = max(1, max_inner_rounds)
        self.budgets = dict(budgets or DEFAULT_BUDGET_TABLE)
        self.usage = usage
        self.models_used = dict(models_used or {})
        self.closeables = tuple(closeables)
        self._limitations: list[str] = []
        self._budget_lock = asyncio.Lock()
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        self._search_calls = 0
        self._fetch_calls = 0
        self._budget_depth = "standard"

    def budget_for(self, depth: str) -> DepthBudget:
        return self.budgets.get(depth, DEFAULT_BUDGET_TABLE["standard"])

    async def _fetch_once(self, record, *, scope, agent, phase):
        # Two investigation seats can discover the same URL before either has
        # finished fetching it. Recheck under a per-evidence lock before charging.
        lock = self._fetch_locks.setdefault(record.pk, asyncio.Lock())
        async with lock:
            current = await self.database.get_evidence(record.task_id, record.local_id) or record
            if not self.evidence.needs_direct_fetch(current):
                return current
            if not await self._reserve_tool("fetch"):
                return None
            return await self.evidence.fetch_one(
                current,
                scope=scope,
                agent=agent,
                phase=phase,
                allow_external_fallback=(
                    current.source_tier <= 3
                    and current.source_role in {"authority", "party", "independent"}
                ),
            )

    async def _review_generated(self, task_id, generated):
        task = await self.database.get_task(task_id)
        if not task or task.investigation_scope not in PROTECTED_SCOPES:
            return generated, False
        if self.scope_reviewer is None:
            return [], True
        self.scope_reviewer.bind(self.database, task_id, task.investigation_scope)
        payloads = [
            {
                "text": c.text,
                "rumor_text": c.rumor_text,
                "correction_text": c.correction_text,
                "analysis_data": c.analysis_data,
            }
            for c in generated
        ]
        texts = []

        def collect(value, key=""):
            if key in {"quote", "support_quote", "url", "origin_url"}:
                return
            if isinstance(value, dict):
                for k, v in value.items():
                    collect(v, k)
            elif isinstance(value, list):
                for v in value:
                    collect(v, key)
            elif isinstance(value, str) and any("\u4e00" <= ch <= "\u9fff" for ch in value):
                texts.append(value)

        for payload in payloads:
            collect(payload)
        texts = list(dict.fromkeys(texts))
        decisions = await self.scope_reviewer.review(texts, kind="claim")
        by_text = dict(zip(texts, decisions, strict=True))

        def sanitize(value, key=""):
            if key in {"quote", "support_quote", "url", "origin_url"}:
                return value
            if isinstance(value, dict):
                return {k: sanitize(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [sanitize(v, key) for v in value]
            if isinstance(value, str) and value in by_text:
                decision = by_text[value]
                if not decision.allowed:
                    raise ValueError("unreviewed content")
                return decision.text
            return value

        accepted = []
        for original, payload in zip(generated, payloads, strict=True):
            try:
                checked = GeneratedClaim.model_validate(
                    {**original.model_dump(), **sanitize(payload)}
                )
                accepted.append(checked)
                await self.scope_reviewer.cache_accepted(checked.text, kind="claim")
            except (ValueError, TypeError):
                continue
        incomplete = sum(d.status == "incomplete" for d in decisions)
        rejected = sum(d.status == "rejected" for d in decisions)
        if incomplete or rejected:
            message = (
                f"范围审查：明确排除 {rejected} 条，尚未完成 {incomplete} 条；成功材料已保留。"
            )
            if message not in self._limitations:
                self._limitations.append(message)
            await self.events.emit(
                task_id,
                "warning",
                {
                    "code": "SCOPE_REVIEW_PARTIAL",
                    "message": message,
                    "rejected": rejected,
                    "incomplete": incomplete,
                    "diagnostics": [d.diagnostic for d in decisions if d.diagnostic],
                },
            )
        return accepted, bool(incomplete)

    def _set_llm_phase_limit(self, token_limit: int, phase: str) -> int:
        """Keep the single task cap while reserving tokens for later phases."""
        baseline = getattr(self, "_token_baseline", 0)
        available = token_limit - baseline
        limit = {
            "investigation": baseline + available * 3 // 5,
            "verification": baseline + available * 9 // 10,
            "comments": baseline + available * 4 // 5,
        }.get(phase, token_limit)
        if self.usage is not None and hasattr(self.usage, "token_limit"):
            if phase == "comments":
                cap = min(
                    token_limit, getattr(self.usage, "absolute_token_limit", None) or token_limit
                )
                used = getattr(self.usage, "tokens_used", baseline)
                limit = min(cap, used + max(0, cap - used) * 4 // 5)
            absolute = getattr(self.usage, "absolute_token_limit", None)
            if isinstance(absolute, int):
                limit = min(limit, absolute)
            self.usage.token_limit = limit
            self.usage.total_token_limit = token_limit
            self.usage.phase = phase
        return limit

    def _llm_phase_has_room(self, minimum: int = 30_000) -> bool:
        limit = getattr(self.usage, "token_limit", None)
        return limit is None or int(getattr(self.usage, "tokens_used", 0)) + minimum < limit

    @staticmethod
    def _quality_progress(report: dict) -> tuple[int, ...]:
        quality = report.get("quality", {})
        blocks = report.get("blocks", [])
        return (
            -len(quality.get("release_gate_missing", [])),
            sum(
                item.get("badge") == "verified"
                for b in blocks
                if b.get("type") == "fact_check_table"
                for item in b.get("items", [])
            ),
            sum(len(b.get("edges", [])) for b in blocks if b.get("type") == "propagation_network"),
            int(quality.get("analysis_items", 0)),
            sum(len(b.get("items", [])) for b in blocks if b.get("type") == "comment_insight"),
        )

    @staticmethod
    def _corroboration_candidates(claims, evidence, max_evidence_per_claim: int):
        """Rank a second independent source for already supported key facts."""

        by_id = {item.local_id: item for item in evidence}
        classifier = bundled_classifier()
        candidates = []
        for claim in claims:
            if (
                claim.agent != "fact_investigator"
                or not claim.is_key
                or claim.verification_state != "complete"
                or claim.badge != "unverified"
                or claim.verdict != "support"
                or claim.independent_sources != 1
                or len(claim.evidence_ids) >= max_evidence_per_claim
            ):
                continue
            publishers = {
                classifier.canonical_publisher(source.source_domain, source.publisher_entity)
                for ref in claim.evidence_ids
                if (source := by_id.get(ref)) is not None
                and source.source_role in {"authority", "independent"}
            }
            compact = re.sub(r"\s+", "", claim.text)
            fragments = {
                compact[pos : pos + 5]
                for pos in range(max(0, len(compact) - 4))
                if not compact[pos : pos + 5].isdigit()
            }
            for source in evidence:
                if (
                    source.local_id in claim.evidence_ids
                    or source.fetch_status != "fetched"
                    or source.source_role not in {"authority", "independent"}
                    or (source.extra or {}).get("scope_status")
                    not in {"main", "foreign_supplement", "event_context"}
                    or not source.published_at
                    or classifier.canonical_publisher(source.source_domain, source.publisher_entity)
                    in publishers
                ):
                    continue
                material = re.sub(r"\s+", "", (source.content_text or "")[:12000])
                score = sum(fragment in material for fragment in fragments)
                if score >= 4:
                    candidates.append((score, claim, source))
        candidates.sort(key=lambda item: (-item[0], item[2].source_tier, item[1].local_id))
        return candidates

    async def _corroborate_key_fact(self, task_id: str, depth: str) -> None:
        """Verify at most three additional relations; never infer support from overlap."""

        key = "report:fact_corroboration"
        state = await self.database.checkpoint(task_id, key) or {}
        if state.get("reason") == "already_definitive":
            return
        task = await self.database.get_task(task_id)
        if not task or task.status != "running":
            return
        claims = await self.database.list_claims(task_id)
        if any(
            claim.agent == "fact_investigator"
            and claim.verification_state == "complete"
            and claim.badge in {"verified", "disputed", "refuted"}
            for claim in claims
        ):
            await self.database.save_checkpoint(
                task_id, key, {"status": "complete", "reason": "already_definitive"}
            )
            return
        evidence = await self.database.list_evidence(task_id)
        budget = self.budget_for(depth)
        attempted = [tuple(pair) for pair in state.get("attempted", [])]
        for _, claim, source in self._corroboration_candidates(
            claims, evidence, budget.max_evidence_per_claim
        ):
            pair = (claim.local_id, source.local_id)
            if pair in attempted or len(attempted) >= 3:
                continue
            if await self._emit_budget(task_id, depth) or not self._llm_phase_has_room(5_000):
                break
            current = await self.database.get_claim(task_id, claim.local_id)
            if not current or current.badge in {"verified", "disputed", "refuted"}:
                continue
            if len(current.evidence_ids) >= budget.max_evidence_per_claim:
                continue
            try:
                linked = await self.database.add_claim(
                    ClaimCreate(
                        task_id=task_id,
                        text=current.text,
                        statement_kind=current.statement_kind,
                        agent=current.agent,
                        round=current.round,
                        section=current.section or "fact_check",
                        is_key=current.is_key,
                        evidence_ids=[source.local_id],
                    ),
                    max_claims=budget.max_claims,
                    max_evidence_per_claim=budget.max_evidence_per_claim,
                )
                reviewed = await self.verification.verify_claim(linked, reuse_completed=True)
                attempted.append(pair)
                await self.database.save_checkpoint(
                    task_id, key, {"status": "partial", "attempted": attempted}
                )
                await self.events.emit(
                    task_id,
                    "verify.progress",
                    {
                        "claim_id": reviewed.local_id,
                        "badge": reviewed.badge,
                        "verification_state": reviewed.verification_state,
                        "scope": "independent_corroboration",
                    },
                )
                if reviewed.badge in {"verified", "disputed", "refuted"}:
                    break
            except Exception as exc:
                self._limitations.append(
                    f"关键事实独立佐证未完成（{type(exc).__name__}），保留原核验结果。"
                )
                break
        await self.database.save_checkpoint(
            task_id, key, {"status": "partial", "attempted": attempted}
        )

    async def _recover_report_gaps(
        self,
        task_id: str,
        event_query: str,
        board: ForumBoard,
        *,
        recovery_round: int = 0,
        missing: Sequence[str] = (),
    ) -> None:
        """One persisted, bounded recovery per deficient investigation chapter."""
        task = await self.database.get_task(task_id)
        if not task or task.status in {"stopping", "failed", "done", "pausing", "paused"}:
            return
        claims = await self.database.list_claims(task_id)
        evidence = await self.database.list_evidence(task_id)
        budget = self.budget_for(task.depth)
        main = {
            e.local_id
            for e in evidence
            if (e.extra or {}).get("scope_status")
            in {"main", "foreign_supplement", "event_context"}
        }
        goals = {}
        usable = [
            c
            for c in claims
            if c.agent == "fact_investigator"
            and c.verification_state == "complete"
            and c.verdict == "support"
            and set(c.evidence_ids) & main
        ]
        if len(usable) < 2 or set(missing) & {
            "main_evidence",
            "verifiable_key_claim",
            "event_timeline",
            "in_window_timeline",
        }:
            goals["fact_investigator"] = (
                "补齐核心事实与机构处置结论：优先重新阅读已经取得的通报全文中间段落，"
                "再补查原始来源。拆成短的归属性陈述，分别说明谁发布什么结论、何时作出什么处置；"
                "不能重复只谈取证工作量或把单方说法改成已证实事实。"
            )
        relation_state = await self.database.checkpoint(task_id, "report:relations") or {}
        if "media_propagation" in missing or not relation_state.get("edges"):
            goals["media_propagation"] = (
                "补查核心争议、原始发布、机构回应及后续跟进的明确引用关系；"
                "优先找原通报和注明来源的转载。每条关系提供支撑原话，"
                "同主题同日报道不构成互相转载关系。"
            )
        evidence_by_id = {item.local_id: item for item in evidence}
        if recovery_round <= 1 and not any(
            c.agent == "history_insight"
            and c.verification_state == "complete"
            and c.verdict == "support"
            and independent_case_evidence(
                c, evidence_by_id, task.resolved_event_query or task.event_query, task.created_at
            )
            for c in claims
        ):
            goals["history_insight"] = (
                "寻找高校优先、必要时跨机构的机制相似独立案例，查明公开处置结果、"
                "相似机制和关键差异；只需关键处置机制可比，不要求指控、诉讼、论文复核、问责"
                "每个环节全部相同。校方已公开处分即属于可陈述结果，但不得推断其后续变化。"
                "不能改成本事件旧报道。只使用本次任务创建前已公开的材料；旧陈述若有新来源，"
                "仍须输出带新证据编号的陈述以重新核验。"
            )
        for name, goal in goals.items():
            if name not in self.agents:
                continue
            key = f"report:recovery:{name}" + (f":{recovery_round}" if recovery_round else "")
            if await self.database.checkpoint(task_id, key):
                continue
            if await self._emit_budget(task_id, task.depth) or not self._llm_phase_has_room():
                self._limitations.append("章节补查受阶段预算限制，剩余缺口保留在报告中。")
                break
            current = await self.database.get_task(task_id)
            if not current or current.status != "running":
                break
            # Reserve before calling: a crash cannot silently spend the same recovery again.
            await self.database.save_checkpoint(
                task_id, key, {"phase": "verified", "attempted": True, "goal": goal}
            )
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": name, "phase": "quality_recovery", "inner_round": 1},
            )
            try:
                await self._run_agent(
                    task_id=task_id,
                    event_query=event_query + "\n【章节缺口专项补查】" + goal,
                    agent_name=name,
                    agent=self.agents[name],
                    board=board,
                    outer_round=budget.outer_rounds + 1,
                    top_k=budget.top_k,
                    inner_limit=1,
                    summary_phase="quality_recovery",
                )
            except Exception as exc:
                message = f"{name} 章节补查未完成（{type(exc).__name__}），保留原材料。"
                self._limitations.append(message)
                diagnostic = upstream_diagnostic(exc, stage="quality_recovery", batch=key)
                await self.database.save_checkpoint(
                    task_id,
                    key,
                    {
                        "phase": "verified",
                        "attempted": True,
                        "goal": goal,
                        "status": "failed",
                        "diagnostic": diagnostic,
                    },
                )
                await self.events.emit(
                    task_id,
                    "warning",
                    {
                        "code": "AGENT_FAILED",
                        "message": message,
                        "agent": name,
                        "diagnostic": diagnostic,
                    },
                )
                await self.events.emit(
                    task_id,
                    "agent.status",
                    {"agent": name, "phase": "failed", "inner_round": 1},
                )
        # Also covers pending claims left by a recovery interrupted before verification.
        claims = await self.database.list_claims(task_id)
        used = sum(
            [
                len(await self.database.claim_evidence_rows(c.pk))
                for c in claims
                if c.verification_state != "pending"
            ]
        )
        retry_usage = await self.database.checkpoint(task_id, "report:verify_retry_usage") or {}
        retry_spent = int(retry_usage.get("spent", 0))
        used += retry_spent
        for claim in claims:
            current = await self.database.get_task(task_id)
            if not current or current.status != "running":
                break
            retry_incomplete = claim.verification_state == "incomplete"
            retry_key = f"report:verify_retry:{claim.local_id}"
            if claim.verification_state != "pending" and not retry_incomplete:
                continue
            if retry_incomplete and await self.database.checkpoint(task_id, retry_key):
                continue
            rows = await self.database.claim_evidence_rows(claim.pk)
            cost = (
                sum(
                    not row["relation"]
                    or str(row["verify_reason"] or "").startswith(("核验失败", "核验未完成"))
                    for row in rows
                )
                if retry_incomplete
                else len(rows)
            )
            if (
                used + cost > budget.max_verify_calls
                or await self._emit_budget(task_id, task.depth)
                or not self._llm_phase_has_room()
            ):
                self._limitations.append("章节补查核验预算不足，未核完的内容不进入已审分析。")
                break
            used += cost
            if retry_incomplete:
                retry_spent += cost
                await self.database.save_checkpoint(
                    task_id, retry_key, {"phase": "verified", "attempted": True}
                )
                await self.database.save_checkpoint(
                    task_id,
                    "report:verify_retry_usage",
                    {"phase": "verified", "spent": retry_spent},
                )
            await self.verification.verify_claim(claim, reuse_completed=retry_incomplete)
        await self._repair_history_comparison(task_id)

    async def _repair_history_comparison(self, task_id: str) -> None:
        """Retry a case fact against its own fetched source after a partial verdict."""
        key = "report:history_repair"
        if await self.database.checkpoint(task_id, key):
            return
        task = await self.database.get_task(task_id)
        agent = self.agents.get("history_insight")
        if not task or not agent or task.status != "running":
            return
        evidence = {item.local_id: item for item in await self.database.list_evidence(task_id)}
        claims = await self.database.list_claims(task_id)
        eligible = [
            claim
            for claim in claims
            if claim.agent == "history_insight"
            and independent_case_evidence(
                claim, evidence, task.resolved_event_query or task.event_query, task.created_at
            )
        ]
        if any(
            claim.verdict == "support" and claim.verification_state == "complete"
            for claim in eligible
        ):
            return
        sources = {
            ref: evidence[ref]
            for claim in eligible
            if claim.verdict in {"partial", "not_mentioned"}
            for ref in independent_case_evidence(
                claim, evidence, task.resolved_event_query or task.event_query, task.created_at
            )
            if evidence[ref].fetch_status == "fetched"
        }
        if (
            not sources
            or await self._emit_budget(task_id, task.depth)
            or not self._llm_phase_has_room()
        ):
            return
        await self.database.save_checkpoint(
            task_id, key, {"attempted": True, "evidence_refs": list(sources)[:2]}
        )
        budget = self.budget_for(task.depth)
        for source in list(sources.values())[:2]:
            if await self._emit_budget(task_id, task.depth) or not self._llm_phase_has_room():
                break
            try:
                generated = await agent.summarize(
                    (task.resolved_event_query or task.event_query)
                    + "\n【历史案例核验纠错】此前历史陈述因日期或复合信息不一致而未获完整支持。"
                    "仅根据下一份来源重新提取一条最短的机构调查或处分结果事实。"
                    "不要写来源正文与页面元数据不一致的具体日期；"
                    "独立案例元数据仍须完整，不能加入其他来源或当前事件。",
                    [source],
                )
            except Exception as exc:
                self._limitations.append(f"历史案例定向纠错未完成（{type(exc).__name__}）。")
                continue
            for item in generated[:3]:
                if item.evidence_ids != [source.local_id] or not (item.analysis_data or {}).get(
                    "historical_case"
                ):
                    continue
                reviewed, incomplete = await self._review_generated(task_id, [item])
                if incomplete:
                    break
                if not reviewed:
                    continue
                item = reviewed[0]
                try:
                    claim = await self.database.add_claim(
                        ClaimCreate(
                            task_id=task_id,
                            text=item.text,
                            agent="history_insight",
                            statement_kind=item.statement_kind,
                            evidence_ids=item.evidence_ids,
                            analysis_data=item.analysis_data,
                            round=budget.outer_rounds + 1,
                            section="history",
                        ),
                        max_claims=budget.max_claims,
                        max_evidence_per_claim=budget.max_evidence_per_claim,
                    )
                    if claim.verification_state == "pending":
                        await self.verification.verify_claim(claim)
                    if (
                        await self.database.get_claim(task_id, claim.local_id)
                    ).verdict == "support":
                        return
                except Exception as exc:
                    self._limitations.append(
                        f"历史案例纠错陈述未入库或核验（{type(exc).__name__}）。"
                    )

    async def _prepare_topic_candidates(
        self, task, *, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict[str, object]]:
        await self.events.emit_task_status(
            task.id, status="running", phase="topic_discovery", progress=3
        )
        outcome = await self._discover_topic(
            task, date_from_override=date_from, date_to_override=date_to
        )
        candidates = [item.model_dump(mode="json") for item in outcome.candidates]
        payload = {
            "phase": "topic_selection",
            "original_query": task.event_query,
            "candidates": candidates,
            "attempts": [item.model_dump(mode="json") for item in outcome.attempts],
            "provider_coverage": outcome.provider_coverage.model_dump(mode="json"),
            "effective_time_range": outcome.effective_time_range.model_dump(mode="json"),
            "used_default_time_range": outcome.used_default_time_range,
            "manual_entry_allowed": True,
        }
        await self.database.save_checkpoint(task.id, "topic:selection", payload)
        await self.events.emit_task_status(
            task.id, status="paused", phase="topic_selection", progress=5
        )
        return candidates

    async def _discover_topic(
        self,
        task,
        manual_query: str | None = None,
        *,
        date_from_override: str | None = None,
        date_to_override: str | None = None,
    ):
        budget = self.budget_for(task.depth)
        outcome = await self.topic_discovery.discover(
            TopicDiscoveryRequest(
                topic=task.event_query,
                manual_event_query=manual_query,
                languages=tuple(task.source_languages),
                source_scope=task.source_scope,
                date_from=date_from_override or task.time_range_from,
                date_to=date_to_override or task.time_range_to,
                max_search_calls=max(0, budget.search_calls - self._search_calls),
                max_fetch_calls=min(10, max(0, budget.fetch_calls - self._fetch_calls)),
            )
        )
        self._search_calls += outcome.search_calls
        self._fetch_calls += outcome.fetch_calls
        if outcome.provider_coverage.message:
            self._limitations.append(outcome.provider_coverage.message)
        for attempt in outcome.attempts:
            await self.events.emit(
                task.id,
                "search.result",
                {
                    "agent": "topic_discovery",
                    "round": attempt.round,
                    "provider": attempt.provider,
                    "query": attempt.query,
                    "language": attempt.language,
                    "hits": attempt.accepted_hits,
                    "raw_hits": attempt.raw_hits,
                    "rejected": attempt.rejected,
                    "status": attempt.status,
                    "error": attempt.error,
                },
            )
        return outcome

    async def _preflight_manual_topic(self, task, query: str) -> bool:
        await self.events.emit_task_status(
            task.id, status="running", phase="topic_preflight", progress=4
        )
        outcome = await self._discover_topic(task, query)
        if outcome.candidates:
            await self.database.save_checkpoint(
                task.id,
                "topic:selected",
                {
                    "phase": "outer",
                    "next_outer_round": 1,
                    "original_query": task.event_query,
                    "resolved_event_query": query,
                    "manual_preflight": {
                        "status": "verified",
                        "candidate": outcome.candidates[0].model_dump(mode="json"),
                    },
                },
            )
            return True
        await self.database.set_resolved_event_query(task.id, None)
        await self.database.save_checkpoint(
            task.id,
            "topic:selection",
            {
                "phase": "topic_selection",
                "original_query": task.event_query,
                "candidates": [],
                "attempts": [item.model_dump(mode="json") for item in outcome.attempts],
                "provider_coverage": outcome.provider_coverage.model_dump(mode="json"),
                "effective_time_range": outcome.effective_time_range.model_dump(mode="json"),
                "used_default_time_range": outcome.used_default_time_range,
                "manual_preflight": {
                    "status": "unverified",
                    "query": query,
                    "message": "没有找到足以确认该具体事件的公开来源。请修改名称、时间或关键词。",
                },
                "manual_entry_allowed": True,
            },
        )
        await self.events.emit_task_status(
            task.id, status="paused", phase="topic_selection", progress=5
        )
        return False

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
        # Comment discovery is downstream of topic confirmation. Never send the
        # original broad topic back into the post selector once a concrete event exists.
        candidate_query = (
            task.resolved_event_query
            or investigation_query.splitlines()[0].strip()
            or task.event_query
        )
        refine_query = getattr(self.comment_evaluator, "refine_query", None)
        if refine_query is not None:
            try:
                candidate_query = await refine_query(candidate_query, context)
            except Exception as exc:
                self._limitations.append(
                    f"评论候选检索词收敛失败（{type(exc).__name__}），已回退到已确认事件。"
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
        previous_limit = getattr(self.usage, "token_limit", None)
        previous_phase = getattr(self.usage, "phase", "unknown")
        self._set_llm_phase_limit(self.budget_for(self._budget_depth).token_limit, "comments")
        try:
            await self._run_comment_insight_impl(task_id, event_query, board)
        finally:
            if self.usage is not None and hasattr(self.usage, "token_limit"):
                self.usage.token_limit = previous_limit
                self.usage.phase = previous_phase

    async def _comment_public_context(self, task_id: str) -> list[dict]:
        """Use the already published core inventory, preserving verdicts and scope."""
        row = await self.database.get_report_for_task(task_id)
        if not row:
            return []
        report = json.loads(row["ir_json"])
        sources = {
            e.local_id: e
            for e in await self.database.list_evidence(task_id)
            if e.kind != "social_comments"
        }
        facts = [
            item
            for block in report["blocks"]
            if block["type"] == "fact_check_table"
            for item in block.get("items", [])
        ]
        result = []
        for block in report["blocks"]:
            if block["type"] != "evidence_appendix":
                continue
            for source in block.get("items", []):
                ref = source.get("evidence_ref")
                if ref not in sources:
                    continue
                if (sources[ref].extra or {}).get("scope_status") == "background":
                    continue
                bound = [
                    f
                    for f in facts
                    if f.get("origin_agent") != "history_insight"
                    and ref in {c.get("evidence_ref") for c in f.get("citations", [])}
                ]
                if not bound:
                    continue
                result.append(
                    {
                        "evidence_ref": ref,
                        "title": source.get("title"),
                        "published_at": source.get("published_at"),
                        "source_role": sources[ref].source_role,
                        "fetch_status": sources[ref].fetch_status,
                        "excerpt": (
                            source.get("original_excerpt")
                            or sources[ref].content_text
                            or sources[ref].snippet
                            or ""
                        )[:1200],
                        "_full_text": sources[ref].content_text or sources[ref].snippet or "",
                        "scope_status": (sources[ref].extra or {}).get("scope_status", "unknown"),
                        "claims": [
                            {
                                k: f.get(k)
                                for k in (
                                    "claim_ref",
                                    "text",
                                    "badge",
                                    "verdict",
                                    "verification_state",
                                )
                            }
                            for f in bound
                        ],
                    }
                )
        task = await self.database.get_task(task_id)
        if task and task.investigation_scope in PROTECTED_SCOPES:
            if not self.scope_reviewer:
                return []
            # Published labels and facts were already reviewed. Source bodies stay
            # local; only selected windows are reviewed before entering a prompt.
            self.scope_reviewer.bind(self.database, task_id, task.investigation_scope)
        return result

    async def _comment_follow_up(
        self, task_id: str, event_query: str, question: dict, *, allow_done=False
    ) -> dict:
        """One public search round; share all normal search/fetch/claim/verify caps."""
        task = await self.database.get_task(task_id)
        if (
            not task
            or task.status not in ({"running", "done"} if allow_done else {"running"})
            or question.get("publicly_verifiable") is not True
        ):
            return {"status": "not_applicable", "evidence": []}
        key = "comments:follow-up:" + question["id"]
        attempts = await self.database.fetch_one(
            "SELECT COUNT(*) AS n FROM task_state WHERE task_id=? AND step_key LIKE 'comments:follow-up:%' AND json_extract(payload,'$.status') != 'budget_limited'",
            (task_id,),
        )
        if attempts and attempts["n"] >= 3:
            return {"status": "round_limit", "evidence": []}
        previous = await self.database.checkpoint(task_id, key)
        if previous and previous.get("status") != "budget_limited":
            return {"status": "round_limit", "evidence": []}
        if await self._emit_budget(task_id, task.depth) or not self._llm_phase_has_room(18_000):
            return {"status": "budget_limited", "evidence": []}
        await self.database.save_checkpoint(
            task_id, key, {"attempted": True, "status": "incomplete"}
        )
        scope = InvestigationScope(
            event_query=event_query,
            languages=tuple(task.source_languages),
            source_scope=task.source_scope,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        query = f"{event_query[:140]} {question['title'][:100]}"
        params = SearchParams(
            query=query,
            top_k=3,
            lang=task.source_languages[0],
            region=_default_region(task.source_languages[0]),
            freshness="noLimit",
            langsearch_contents_text=False,
        )

        def accept(result):
            decision = scope.classify_result(result, agent="fact_investigator", search_query=query)
            return decision.accepted, decision.reasons

        search_filtered = getattr(self.search, "search_filtered", None)
        search_reserved, search_limited = 0, False

        async def reserve_search():
            nonlocal search_reserved, search_limited
            allowed = await self._reserve_tool("search")
            search_reserved += int(allowed)
            search_limited |= not allowed
            return allowed

        if callable(search_filtered):
            results = await search_filtered(
                params,
                accept,
                before_call=reserve_search,
                min_source_groups=1,
            )
        else:
            if not await reserve_search():
                await self.database.save_checkpoint(
                    task_id, key, {"attempted": False, "status": "budget_limited"}
                )
                return {"status": "budget_limited", "evidence": []}
            results = await self.search.search(params)
        if not search_reserved and search_limited:
            await self.database.save_checkpoint(
                task_id, key, {"attempted": False, "status": "budget_limited"}
            )
            return {"status": "budget_limited", "evidence": []}
        accepted = []
        for result in results[:3]:
            decision = scope.classify_result(result, agent="fact_investigator", search_query=query)
            if decision.accepted:
                accepted.append(
                    result.model_copy(update={"raw": {**result.raw, "_scope": decision.as_extra()}})
                )
        records = await self.evidence.add_search_results(task_id, query, accepted)
        for index, record in enumerate(records):
            records[index] = (
                await self._fetch_once(
                    record, scope=scope, agent="fact_investigator", phase="primary"
                )
                or record
            )
            await self.events.emit(
                task_id,
                "evidence.added",
                {"evidence_id": record.local_id, "title": record.title, "agent": "comment_insight"},
            )
        agent = self.agents.get("fact_investigator")
        budget = self.budget_for(task.depth)
        new_claim_ids = set()
        claim_limited, verification_limited = False, False
        if agent and records and self._llm_phase_has_room(12_000):
            generated = await agent.summarize(
                event_query
                + "\n【评论问题定向取证】"
                + question["title"]
                + "。仅提取回答该公共问题的最短事实，保留时间条件；评论不构成事实证据。",
                records,
            )
            reviewed, _ = await self._review_generated(task_id, generated[:2])
            used = sum(
                [
                    len(await self.database.claim_evidence_rows(c.pk))
                    for c in await self.database.list_claims(task_id)
                    if c.verification_state != "pending"
                ]
            )
            retry_usage = await self.database.checkpoint(task_id, "report:verify_retry_usage") or {}
            used += int(retry_usage.get("spent", 0))
            for item in reviewed:
                if used + len(item.evidence_ids) > budget.max_verify_calls:
                    verification_limited = True
                    continue
                if (
                    item.statement_kind != "fact"
                    or not item.evidence_ids
                    or not set(item.evidence_ids) <= {e.local_id for e in records}
                ):
                    continue
                if not self._llm_phase_has_room(8_000):
                    break
                try:
                    claim = await self.database.add_claim(
                        ClaimCreate(
                            task_id=task_id,
                            text=item.text,
                            statement_kind=item.statement_kind,
                            agent="fact_investigator",
                            round=budget.outer_rounds + 1,
                            section="fact_check",
                            evidence_ids=item.evidence_ids,
                        ),
                        max_claims=budget.max_claims,
                        max_evidence_per_claim=budget.max_evidence_per_claim,
                    )
                except ValueError as exc:
                    if not str(exc).startswith("任务 claim 总数已达当前深度上限"):
                        raise
                    self._limitations.append(
                        "评论补查取得材料，但陈述额度不足，新增事实未完成入库与核验。"
                    )
                    claim_limited = True
                    continue
                used += len(item.evidence_ids)
                await self.verification.verify_claim(claim, reuse_completed=True)
                new_claim_ids.add(claim.local_id)
                await self.events.emit(
                    task_id,
                    "claim.added",
                    {
                        "claim_id": claim.local_id,
                        "text": claim.text,
                        "evidence_ids": claim.evidence_ids,
                        "agent": "comment_insight",
                    },
                )
        claims = await self.database.list_claims(task_id)
        context = []
        for source in records:
            texts = [source.title, (source.content_text or source.snippet or "")[:1200]]
            if task.investigation_scope in PROTECTED_SCOPES:
                if not self.scope_reviewer:
                    continue
                decisions = await self.scope_reviewer.review(texts, kind="report_text")
                if not all(d.allowed for d in decisions):
                    continue
                texts = [d.text for d in decisions]
            context.append(
                {
                    "evidence_ref": source.local_id,
                    "title": texts[0],
                    "excerpt": texts[1],
                    "_full_text": source.content_text or source.snippet or "",
                    "published_at": source.published_at,
                    "fetch_status": source.fetch_status,
                    "claims": [
                        {
                            "claim_ref": c.local_id,
                            "text": c.text,
                            "badge": c.badge,
                            "verdict": c.verdict,
                            "verification_state": c.verification_state,
                        }
                        for c in claims
                        if c.local_id in new_claim_ids
                        and source.local_id in c.evidence_ids
                        and c.verification_state == "complete"
                    ],
                }
            )
        outcome = (
            "claim_budget_limited"
            if claim_limited
            else "verification_budget_limited"
            if verification_limited
            else "budget_exhausted"
            if search_limited
            else "complete"
            if context
            else "no_new_evidence"
        )
        await self.database.save_checkpoint(
            task_id,
            key,
            {
                "attempted": True,
                "status": outcome,
                "evidence_refs": [e["evidence_ref"] for e in context],
            },
        )
        return {"status": outcome, "evidence": context}

    async def _run_comment_insight_impl(
        self, task_id: str, event_query: str, board: ForumBoard
    ) -> None:
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
        if hasattr(self.comment_agent, "analyze"):
            checkpoint = await self.database.checkpoint(task_id, "comments:analysis")
            sources = {(e.extra or {}).get("collection_id"): e.local_id for e in comment_evidence}
            rows = await self.database.fetch_all(
                "SELECT * FROM social_comment WHERE task_id=? ORDER BY collection_id,created_at,id",
                (task_id,),
            )
            samples = [
                {**dict(row), "evidence_ref": sources[row["collection_id"]]}
                for row in rows
                if row["collection_id"] in sources
            ]
            task = await self.database.get_task(task_id)
            scope_excluded = 0
            scope_pending = 0
            scope_diagnostics = []
            if task and task.investigation_scope in PROTECTED_SCOPES:
                if self.scope_reviewer:
                    self.scope_reviewer.bind(self.database, task_id, task.investigation_scope)
                    phase_cap = getattr(self.usage, "token_limit", None)
                    if phase_cap is not None:
                        used = self.usage.tokens_used
                        self.usage.token_limit = used + max(0, phase_cap - used) // 2
                    try:
                        decisions = await self.scope_reviewer.review(
                            [str(sample["text"]) for sample in samples], kind="comment"
                        )
                    finally:
                        if phase_cap is not None:
                            self.usage.token_limit = phase_cap
                    scope_excluded = sum(d.status == "rejected" for d in decisions)
                    scope_pending = sum(d.status == "incomplete" for d in decisions)
                    scope_diagnostics = list(
                        {
                            json.dumps(d.diagnostic, sort_keys=True): d.diagnostic
                            for d in decisions
                            if d.diagnostic
                        }.values()
                    )
                    samples = [
                        {**sample, "text": d.text}
                        for sample, d in zip(samples, decisions, strict=True)
                        if d.allowed
                    ]
                else:
                    scope_pending = len(samples)
                    samples = []

            async def can_continue():
                task = await self.database.get_task(task_id)
                return bool(
                    task
                    and task.status not in {"stopping", "failed", "pausing", "paused"}
                    and not await self._emit_budget(task_id, task.depth)
                    and self._llm_phase_has_room(12_000)
                )

            async def save_progress(analysis):
                await self.database.save_checkpoint(
                    task_id, "comments:analysis", {"phase": "comments_ready", "analysis": analysis}
                )

            async def save_review_decision(key, decision):
                await self.database.save_checkpoint(
                    task_id, f"comments:theme-review:{key}", decision
                )

            analysis_method = getattr(
                self.comment_agent,
                "analyze_quick_read",
                getattr(self.comment_agent, "analyze_questions", self.comment_agent.analyze),
            )
            extra = {}
            if hasattr(self.comment_agent, "analyze_quick_read"):
                extra["review_policy_version"] = SCOPE_POLICY_VERSION
                extra["public_context_fingerprint"] = fingerprint(
                    await self._comment_public_context(task_id)
                )
                if self.scope_reviewer and task and task.investigation_scope in PROTECTED_SCOPES:
                    extra["review_observations"] = lambda texts: self.scope_reviewer.review(
                        texts, kind="report_text"
                    )
            elif hasattr(self.comment_agent, "analyze_questions"):
                extra = {
                    "public_context": await self._comment_public_context(task_id),
                    "follow_up": lambda question: self._comment_follow_up(
                        task_id, event_query, question
                    ),
                }
                if self.scope_reviewer and task and task.investigation_scope in PROTECTED_SCOPES:
                    extra["review_observations"] = lambda texts: self.scope_reviewer.review(
                        texts, kind="report_text"
                    )
            analysis = await analysis_method(
                event_query,
                samples,
                can_continue=can_continue,
                previous=(checkpoint or {}).get("analysis"),
                save_progress=save_progress,
                save_review_decision=save_review_decision,
                investigation_scope=task.investigation_scope if task else "general",
                **extra,
            )
            if scope_excluded:
                coverage = analysis.setdefault("coverage", {})
                coverage["collected"] = int(coverage.get("collected", 0)) + scope_excluded
                coverage["scope_excluded"] = int(coverage.get("scope_excluded", 0)) + scope_excluded
                warning = f"范围审查明确排除 {scope_excluded} 条评论，未计入主题分析。"
                warnings = analysis.setdefault("warnings", [])
                if warning not in warnings:
                    warnings.append(warning)
            if scope_pending:
                analysis.setdefault("coverage", {})["scope_review_incomplete"] = scope_pending
                analysis["coverage"]["collected"] = len(rows)
                analysis.setdefault("warnings", []).append(
                    f"{scope_pending} 条评论尚未完成隐私审查，暂不展示，不计为违规评论。"
                )
                analysis["status"] = (
                    "partial" if analysis.get("items") or analysis.get("observations") else "failed"
                )
                analysis.setdefault("diagnostics", []).extend(scope_diagnostics)
                reasons = sorted({d["message"] for d in scope_diagnostics})
                if reasons:
                    analysis["warnings"].append("评论范围审查未完成原因：" + "；".join(reasons))
            analysis_status = analysis.get("status", "partial")
            await self._post(
                board,
                ForumMessageCreate(
                    task_id=task_id,
                    round=1,
                    agent="comment_insight",
                    type="summary",
                    content=(
                        "评论样本分析完成"
                        if analysis_status == "complete"
                        else "评论样本分析部分完成"
                        if analysis.get("items") or analysis.get("observations")
                        else "评论样本尚未形成通过审查的主题，可恢复分析"
                    )
                    + "；计数与引用仅适用于已采集样本。",
                    refs=sorted(
                        {
                            e
                            for item in [*analysis["items"], *analysis.get("observations", [])]
                            for e in item["evidence_refs"]
                        }
                    ),
                    payload={
                        "sampling_scope": "已确认帖子的脱敏样本，不代表总体民意",
                        "comment_analysis": {
                            k: v for k, v in analysis.items() if not k.startswith("_")
                        },
                    },
                ),
            )
            await self.database.save_checkpoint(
                task_id, "comments:analysis", {"phase": "comments_ready", "analysis": analysis}
            )
            await self.events.emit(
                task_id,
                "agent.status",
                {
                    "agent": "comment_insight",
                    "phase": "done" if analysis_status == "complete" else "partial",
                    "inner_round": 1,
                },
            )
            return
        generated = await self.comment_agent.summarize(event_query, comment_evidence)
        task = await self.database.get_task(task_id)
        generated, _ = await self._review_generated(task_id, generated)
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
                "search_calls": self._search_calls,
                "fetch_calls": self._fetch_calls,
                "timing": await task_timing(self.database, task_id),
                "tokens_reserved": int(getattr(self.usage, "_tokens_reserved", 0)),
                "phase_token_limit": getattr(self.usage, "token_limit", None),
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
        inner_limit: int | None = None,
        summary_phase: str | None = None,
    ) -> dict[str, object]:
        digest = (
            board.digest_for(agent_name, outer_round - 1)
            if outer_round > 1 and summary_phase != "quality_recovery"
            else ""
        )
        scoped_query = event_query + (f"\n主持人/论坛补充：{digest}" if digest else "")

        async def warn_rate_limit(stage: str, exc: RateLimitError) -> str:
            label = {"plan": "规划", "summarize": "陈述生成", "reflect": "反思"}[stage]
            message = (
                f"{agent_name} 的{label}调用受限（{sanitize_upstream_message(exc)}）；"
                "已保留此前入库的材料和陈述，本轮不再追加模型调用。"
            )
            self._limitations.append(message)
            await self.events.emit(
                task_id,
                "warning",
                {
                    "code": {
                        "plan": "AGENT_PLAN_RATE_LIMITED",
                        "summarize": "AGENT_SUMMARY_RATE_LIMITED",
                        "reflect": "AGENT_REFLECTION_RATE_LIMITED",
                    }[stage],
                    "message": message,
                    "agent": agent_name,
                    "stage": stage,
                    "rate_limit": rate_limit_diagnostic(exc),
                },
            )
            return message

        async def warn_phase_budget(stage: str, exc=None) -> str:
            label = {"plan": "规划", "summarize": "陈述生成", "reflect": "反思"}[stage]
            used = int(getattr(self.usage, "tokens_used", 0))
            task_limit = self.budget_for(self._budget_depth).token_limit
            phase_limit = int(getattr(self.usage, "token_limit", 0) or task_limit * 3 // 5)
            budget_detail = getattr(exc, "diagnostic", {}) or (
                self.usage.budget_diagnostic() if hasattr(self.usage, "budget_diagnostic") else {}
            )
            message = (
                f"{agent_name} 的{label}无法预留下一次模型调用所需额度"
                f"（已结算 {used:,}，在途预留 {int(budget_detail.get('tokens_reserved', 0)):,}，本次预计需要 {int(budget_detail.get('required_tokens', 0)):,}，调查阶段上限 {phase_limit:,}，"
                f"任务总上限 {task_limit:,} token）；"
                "已保留当前证据与陈述，余量用于核验和报告。"
            )
            self._limitations.append(message)
            await self.events.emit(
                task_id,
                "warning",
                {
                    "code": "AGENT_BUDGET_RESERVED",
                    "message": message,
                    "agent": agent_name,
                    "stage": stage,
                    "tokens_used": used,
                    "phase_token_limit": phase_limit,
                    "task_token_limit": task_limit,
                    "budget": getattr(exc, "diagnostic", {})
                    or (
                        self.usage.budget_diagnostic()
                        if hasattr(self.usage, "budget_diagnostic")
                        else {}
                    ),
                },
            )
            return message

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
        try:
            plan = await agent.plan(scoped_query)
        except LLMBudgetExhausted as exc:
            await warn_phase_budget("plan", exc)
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": agent_name, "phase": "budget_reserved", "inner_round": 1},
            )
            return {"agent": agent_name, "status": "partial"}
        except RateLimitError as exc:
            await warn_rate_limit("plan", exc)
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": agent_name, "phase": "blocked", "inner_round": 1},
            )
            return {"agent": agent_name, "status": "partial"}
        task = await self.database.get_task(task_id)
        requested_languages = task.source_languages if task else ["zh", "en"]
        search_languages = list(requested_languages)
        if search_phase == "foreign_supplement" and "en" not in search_languages:
            search_languages.append("en")
        plan = ensure_requested_languages(plan, search_languages, event_query)
        if agent_name == "history_insight" and "【章节缺口专项补查】" in event_query:
            current_facts = " ".join(
                item.text
                for item in await self.database.list_claims(task_id)
                if item.agent in {"fact_investigator", "media_propagation"}
            )
            cutoff_year = int(task.created_at[:4]) if task else 2026
            recovery_queries = []
            if "处分" in current_facts and ("法院" in current_facts or "判决" in current_facts):
                recovery_queries.append(
                    SearchQuery(
                        query=(
                            "高校 学生处分 法院判决撤销 重新作出处分 官方裁判文书 "
                            f"{cutoff_year - 2}"
                        ),
                        language="zh",
                        region="CN",
                    )
                )
            if "处分" in current_facts and ("网络" in current_facts or "社交平台" in current_facts):
                recovery_queries.append(
                    SearchQuery(
                        query=(
                            f"高校 学生 网络公开指控 他人 校方调查 处分 情况通报 {cutoff_year - 3}"
                        ),
                        language="zh",
                        region="CN",
                    )
                )
            if recovery_queries:
                plan = plan.model_copy(update={"queries": [*recovery_queries, *plan.queries]})
        scope = InvestigationScope(
            # Topic tasks keep the user's broad input in task.event_query for
            # provenance.  Investigation relevance must use the event the user
            # actually selected, otherwise any page mentioning the institution can
            # enter the evidence inventory.
            event_query=(task.resolved_event_query or event_query) if task else event_query,
            languages=tuple(requested_languages),
            source_scope=task.source_scope if task else "auto",
            date_from=task.time_range_from if task else None,
            date_to=task.time_range_to if task else None,
        )
        prior_claims = await self.database.list_claims(task_id)
        if agent_name == "history_insight":
            current_evidence = {
                item.local_id: item for item in await self.database.list_evidence(task_id)
            }
            claims_before = {
                item.text
                for item in prior_claims
                if item.agent != "history_insight"
                or independent_case_evidence(
                    item,
                    current_evidence,
                    task.resolved_event_query or task.event_query,
                    task.created_at,
                )
            }
        else:
            claims_before = {item.text for item in prior_claims}
        findings: list[str] = []
        last_reason = "达到小 Loop 上限"
        model_limited = False
        for inner_round in range(1, (inner_limit or self.max_inner_rounds) + 1):
            query_limit = self.budget_for(self._budget_depth).queries_per_round
            queries = plan.queries[: max(query_limit, len(search_languages))]
            if (
                agent_name == "history_insight"
                and task
                and (task.time_range_from or task.time_range_to)
                and queries
                and not any(item.scope == "context" for item in queries)
            ):
                # A malformed or fallback plan must still give the history seat
                # one search outside the user's preferred event window.
                queries[-1] = queries[-1].model_copy(update={"scope": "context"})
            for query_item in queries:
                query = (
                    query_item
                    if isinstance(query_item, SearchQuery)
                    else SearchQuery(query=str(query_item), language="zh", region="CN")
                )
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
                params = SearchParams(
                    query=query.query,
                    top_k=top_k,
                    freshness=(
                        f"{task.time_range_from}..{task.time_range_to}"
                        if task
                        and task.time_range_from
                        and task.time_range_to
                        and query.scope == "window"
                        else "noLimit"
                    ),
                    lang=query.language,
                    region=query.region,
                    langsearch_contents_text=False,
                    allow_freshness_fallback=True,
                )
                rejected_reasons: dict[str, int] = {}

                def classify_for_scope(
                    result,
                    reason_counts=rejected_reasons,
                    query_text=query.query,
                ):
                    decision = scope.classify_result(
                        result,
                        agent=agent_name,
                        phase=search_phase,
                        search_query=query_text,
                    )
                    if not decision.accepted:
                        for reason in decision.reasons:
                            reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    return decision

                search_filtered = getattr(self.search, "search_filtered", None)
                if callable(search_filtered):

                    def source_group_for_result(item: SearchResult) -> str:
                        host = (urlsplit(item.url).hostname or "").lower()
                        return self.evidence.classifier.entity_for(host) or registrable_domain(host)

                    async def reserve_provider_call() -> bool:
                        allowed = await self._reserve_tool("search")
                        if not allowed:
                            limitation = (
                                f"全局搜索调用已达 {self._search_calls} 次上限，停止新增检索。"
                            )
                            if limitation not in self._limitations:
                                self._limitations.append(limitation)
                        return allowed

                    def accept_for_scope(item: SearchResult):
                        decision = classify_for_scope(item)
                        return decision.accepted, decision.reasons

                    raw_results = await search_filtered(
                        params,
                        accept_for_scope,
                        before_call=reserve_provider_call,
                        min_source_groups=2 if agent_name != "history_insight" else 1,
                        source_group=source_group_for_result,
                    )
                else:
                    if not await self._reserve_tool("search"):
                        self._limitations.append(
                            f"全局搜索调用已达 {self._search_calls} 次上限，停止新增检索。"
                        )
                        break
                    provider_results = await self.search.search(params)
                    raw_results = [
                        item for item in provider_results if classify_for_scope(item).accepted
                    ]
                results = []
                for result in raw_results:
                    decision = scope.classify_result(
                        result,
                        agent=agent_name,
                        phase=search_phase,
                        search_query=query.query,
                    )
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
                continued_from = getattr(self.search, "last_continued_from", None)
                provider_name = getattr(self.search, "last_provider", self.search.name)
                provider_diagnostics = getattr(self.search, "last_diagnostics", [])
                raw_hits = (
                    sum(
                        int(item.get("count") or 0)
                        for item in provider_diagnostics
                        if isinstance(item, dict)
                    )
                    if provider_diagnostics
                    else len(raw_results)
                )
                await self.events.emit(
                    task_id,
                    "search.result",
                    {
                        "agent": agent_name,
                        "provider": provider_name,
                        "query": query.query,
                        "language": query.language,
                        "hits": len(results),
                        "raw_hits": raw_hits,
                        "rejected": rejected_reasons,
                        "degraded_from": degraded_from,
                        "continued_from": continued_from,
                        "provider_diagnostics": provider_diagnostics,
                    },
                )
                if any(item.get("status") == "budget_exhausted" for item in provider_diagnostics):
                    break
                ordered_records = sorted(
                    records,
                    key=lambda item: (
                        item.source_tier,
                        {"authority": 0, "party": 1, "independent": 2}.get(item.source_role, 3),
                        item.local_id,
                    ),
                )
                for record in ordered_records:
                    updated = record
                    if self.evidence.needs_direct_fetch(record):
                        updated = await self._fetch_once(
                            record, scope=scope, agent=agent_name, phase=search_phase
                        )
                        if updated is None:
                            self._limitations.append(
                                f"全局原文抓取已达 {self._fetch_calls} 次上限，剩余材料保留摘要。"
                            )
                            break
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
                    or (
                        (item.extra or {}).get("scope_status") == "history"
                        and item.published_at
                        and (not task or str(item.published_at)[:10] <= task.created_at[:10])
                    )
                ]
            else:
                inventory = [
                    item
                    for item in complete_inventory
                    if (item.extra or {}).get("scope_status")
                    in {"main", "foreign_supplement", "event_context"}
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
            if agent_name == "history_insight":
                current_evidence = {item.local_id: item for item in inventory}
                existing_claims = [
                    item
                    for item in existing_claims
                    if item.agent != "history_insight"
                    or independent_case_evidence(
                        item,
                        current_evidence,
                        task.resolved_event_query or task.event_query,
                        task.created_at,
                    )
                ]
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
            new_claim_ids: list[str] = []

            async def persist_generated(
                generated, inventory=inventory, new_claim_ids=new_claim_ids
            ):
                evidence_by_id = {item.local_id: item for item in inventory}
                agent_budget = self.budget_for(self._budget_depth)
                generated, review_incomplete = await self._review_generated(task_id, generated)
                accepted_claims = [True] * len(generated)
                for item, scope_allowed in zip(generated, accepted_claims, strict=True):
                    if not scope_allowed:
                        continue
                    if item.text in claims_before:
                        existing = next(
                            (
                                c
                                for c in await self.database.list_claims(task_id)
                                if c.text == item.text
                            ),
                            None,
                        )
                        if existing is None or existing.agent != agent_name:
                            continue
                        new_refs = set(item.evidence_ids) - set(existing.evidence_ids)
                        if (
                            not new_refs
                            or len(new_refs | set(existing.evidence_ids))
                            > agent_budget.max_evidence_per_claim
                        ):
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
                return review_incomplete

            analysis_goal = (
                event_query.split("【章节缺口专项补查】", 1)[-1]
                if summary_phase == "quality_recovery"
                else "primary"
            )
            if summary_phase != "quality_recovery" and outer_round > 1:
                directives = sorted(
                    {
                        message.content.strip()
                        for message in board.history(round_number=outer_round - 1)
                        if message.type == "directive"
                        and (message.payload or {}).get("agent") == agent_name
                    }
                )
                if directives:
                    analysis_goal = (
                        "directive:"
                        + hashlib.sha256(
                            json.dumps(directives, ensure_ascii=False).encode()
                        ).hexdigest()
                    )
            analysis_goal = (
                f"public-event-v1:{task.investigation_scope if task else 'general'}:{analysis_goal}"
            )

            async def persist_batch(
                batch_claims, fingerprint, evidence_ids, analysis_goal=analysis_goal
            ):
                raw = [item.model_dump(mode="json") for item in batch_claims]
                await self.database.save_analysis_batch(
                    task_id,
                    agent_name,
                    fingerprint,
                    {
                        "claims": raw,
                        "pending_review": True,
                        "evidence_ids": evidence_ids,
                        "goal": analysis_goal,
                    },
                )
                incomplete = await persist_generated(batch_claims)
                await self.database.save_analysis_batch(
                    task_id,
                    agent_name,
                    fingerprint,
                    {
                        "claims": raw,
                        "pending_review": incomplete,
                        "evidence_ids": evidence_ids,
                        "goal": analysis_goal,
                    },
                )
                return incomplete

            async def load_batch(fingerprint):
                return await self.database.get_analysis_batch(task_id, agent_name, fingerprint)

            async def list_pending(analysis_goal=analysis_goal):
                rows = await self.database.fetch_all(
                    "SELECT fingerprint,payload FROM analysis_batch WHERE task_id=? AND agent=? ORDER BY rowid",
                    (task_id, agent_name),
                )
                batches = [(row["fingerprint"], json.loads(row["payload"])) for row in rows]
                return [
                    (key, value)
                    for key, value in batches
                    if value.get("pending_review") and value.get("goal") == analysis_goal
                ]

            async def save_processed(fingerprint):
                await self.database.save_analysis_batch(
                    task_id, agent_name, fingerprint, {"complete": True}
                )

            try:
                if hasattr(agent, "_summarize_uncached"):
                    generated = await agent.summarize(
                        summarize_query,
                        inventory,
                        on_batch=persist_batch,
                        load_batch=load_batch,
                        save_processed=save_processed,
                        analysis_goal=analysis_goal,
                        list_pending=list_pending,
                    )
                    if agent.summary_incomplete:
                        model_limited = True
                        diagnostic = agent.summary_incomplete
                        await self.events.emit(
                            task_id,
                            "warning",
                            {
                                "code": "AGENT_SUMMARY_INCOMPLETE",
                                "agent": agent_name,
                                "stage": "summarize",
                                "message": "陈述生成部分完成，已保存成功批次；"
                                + diagnostic["message"],
                                "diagnostic": diagnostic,
                            },
                        )
                else:
                    generated = await agent.summarize(summarize_query, inventory)
                    await persist_generated(generated)
            except LLMBudgetExhausted as exc:
                model_limited = True
                last_reason = await warn_phase_budget("summarize", exc)
                break
            except RateLimitError as exc:
                model_limited = True
                last_reason = await warn_rate_limit("summarize", exc)
                break
            if model_limited:
                break
            if getattr(agent, "summary_no_new_material", False) and not new_claim_ids:
                last_reason = "材料已处理，本轮没有新增可分析输入"
                break
            claims = await self.database.list_claims(task_id)
            try:
                reflection = await agent.reflect(scoped_query, claims)
            except LLMBudgetExhausted as exc:
                model_limited = True
                message = await warn_phase_budget("reflect", exc)
                reflection = Reflection(
                    new_key_findings=[],
                    remaining_gaps=[message],
                    next_queries=[],
                    should_continue=False,
                    reason="调查阶段模型预算已满，保留已有陈述并转入核验。",
                )
            except RateLimitError as exc:
                model_limited = True
                message = await warn_rate_limit("reflect", exc)
                reflection = Reflection(
                    new_key_findings=[],
                    remaining_gaps=[message],
                    next_queries=[],
                    should_continue=False,
                    reason="反思模型受限，已停止追加检索并保留已有陈述。",
                )
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
        summary = "；".join(findings[-8:]) or (
            f"本轮未形成可引用的新陈述；原因：{last_reason[:180]}。"
        )
        finding_claims = [
            claim
            for claim in await self.database.list_claims(task_id)
            if claim.agent == agent_name and claim.text in findings[-8:]
        ]
        summary_items = [
            {
                "text": claim.text,
                "claim_ref": claim.local_id,
                "evidence_refs": claim.evidence_ids,
            }
            for claim in finding_claims
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
                    "phase": summary_phase or "forum",
                    "stop_reason": last_reason,
                    "summary_items": summary_items,
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
        return {
            "agent": agent_name,
            "status": "success" if findings and not model_limited else "partial",
        }

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
                    {
                        "code": "AGENT_FAILED",
                        "message": text,
                        "agent": name,
                        "diagnostic": upstream_diagnostic(exc, stage="investigation", batch=name),
                    },
                )
                await self.events.emit(
                    task_id, "agent.status", {"agent": name, "phase": "blocked", "inner_round": 0}
                )
        return results

    async def _moderate(
        self,
        task_id: str,
        event_query: str,
        board: ForumBoard,
        outer_round: int,
        *,
        allow_next_round: bool,
    ) -> ModeratorReview:
        review = (
            ModeratorReview(
                release=False,
                reason="调查阶段模型预算已预留给核验与报告；主持人未批准结束，后续按发布门评估。",
                unresolved_critical=["独立核验与报告质量评估尚未完成。"],
                degraded=True,
                diagnostics=["phase_budget_reserved"],
            )
            if not self._llm_phase_has_room()
            else await self.moderator.review(
                event_query,
                board.history(),
                len(await self.database.list_evidence(task_id)),
                len(await self.database.list_claims(task_id)),
            )
        )
        if review.release and (
            review.unresolved_critical or any(gap.priority == "high" for gap in review.gaps)
        ):
            review = review.model_copy(
                update={
                    "release": False,
                    "reason": review.reason + "；仍有高优先级缺口，主持人放行未生效。",
                }
            )
        # A release or a hard stop cannot issue instructions for a nonexistent
        # next forum round. Post-verification recovery has its own explicit goal.
        if review.release or not allow_next_round:
            review = review.model_copy(update={"directives": []})
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

    async def _seed_selected_sources(self, task) -> None:
        if await self.database.checkpoint(task.id, "topic:sources_seeded"):
            return
        selection = await self.database.checkpoint(task.id, "topic:selected") or {}
        candidate = selection.get("selected_candidate") or (
            (selection.get("manual_preflight") or {}).get("candidate")
        )
        if not isinstance(candidate, dict):
            return
        sources = candidate.get("sources") or []
        if not isinstance(sources, list):
            return
        scope = InvestigationScope(
            event_query=task.resolved_event_query or task.event_query,
            languages=tuple(task.source_languages),
            source_scope=task.source_scope,
            date_from=task.time_range_from,
            date_to=task.time_range_to,
        )
        summary = str(candidate.get("summary") or candidate.get("title") or "")[:420]
        priority = {"authority": 0, "party": 1, "independent": 2}
        ordered = sorted(
            (item for item in sources if isinstance(item, dict)),
            key=lambda item: priority.get(str(item.get("role")), 3),
        )
        seen_urls: set[str] = set()
        seeded = 0
        for item in ordered:
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls or seeded >= 6:
                continue
            seen_urls.add(url)
            try:
                published = (
                    datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
                    if item.get("published_at")
                    else None
                )
                result = SearchResult(
                    url=url,
                    title=str(item.get("title") or candidate.get("title") or url),
                    snippet=summary,
                    published_at=published,
                    source_name=str(item.get("source_name") or "") or None,
                    provider=str(item.get("provider") or "topic_discovery"),
                    raw={"date_provenance": "search_provider"},
                )
            except ValueError:
                continue
            decision = scope.classify_result(result, agent="fact_investigator")
            if not decision.accepted:
                continue
            result = result.model_copy(
                update={"raw": {**result.raw, "_scope": decision.as_extra()}}
            )
            record = (
                await self.evidence.add_search_results(task.id, task.resolved_event_query, [result])
            )[0]
            if self.evidence.needs_direct_fetch(record) and await self._reserve_tool("fetch"):
                record = await self.evidence.fetch_one(
                    record,
                    scope=scope,
                    agent="fact_investigator",
                    allow_external_fallback=(
                        record.source_tier <= 3
                        and record.source_role in {"authority", "party", "independent"}
                    ),
                )
            institution = str(item.get("source_name") or "").strip()
            host = urlsplit(record.url).hostname or ""
            if (
                item.get("role") == "party"
                and len(institution) >= 4
                and institution in scope.event_query
                and self.evidence.classifier.institution_for(host) == institution
                and record.fetch_status == "fetched"
            ):
                await self.database.mark_selected_institution_source(
                    task.id, record.local_id, institution
                )
                record = await self.database.get_evidence(task.id, record.local_id) or record
            seeded += 1
            await self.events.emit(
                task.id,
                "evidence.added",
                {
                    "evidence_id": record.local_id,
                    "title": record.title,
                    "source_name": record.source_name or record.source_domain,
                    "source_tier": record.source_tier,
                    "published_at": record.published_at,
                    "agent": "topic_discovery",
                },
            )
        await self.database.save_checkpoint(
            task.id,
            "topic:sources_seeded",
            {"phase": "outer", "next_outer_round": 1, "seeded_sources": seeded},
        )

    async def _verify_claims(self, task_id, claims, total):
        """Publish each persisted result immediately; cancel siblings on failure/stop."""
        semaphore = asyncio.Semaphore(4)

        async def verify_one(claim):
            async with semaphore:
                verified = await self.verification.verify_claim(claim)
                await self.events.emit(
                    task_id,
                    "verify.progress",
                    {
                        "claim_id": verified.local_id,
                        "badge": verified.badge,
                        "verification_state": verified.verification_state,
                        "total": total,
                    },
                )
                return verified

        workers = [asyncio.create_task(verify_one(claim)) for claim in claims]
        try:
            return await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

    async def deepen_comment_question(self, task_id: str, question_id: str, *, follow_up=False):
        from yuqing.services.comment_deepening import deepen_comment_question

        return await deepen_comment_question(self, task_id, question_id, follow_up=follow_up)

    async def _bind_task_runtime(self, task):
        task_id = task.id
        if self.scope_reviewer:
            self.scope_reviewer.bind(self.database, task_id, task.investigation_scope)
        if self.usage is not None and hasattr(self.usage, "record_call"):
            learn = getattr(self.usage, "learn_output_budgets", None)
            if callable(learn):
                rows = await self.database.fetch_all(
                    "SELECT payload FROM llm_call ORDER BY rowid DESC LIMIT 512"
                )
                learn([json.loads(row["payload"]) for row in rows])

            async def record_call(value):
                await self.database.record_llm_call(task_id, value)
                await self.database.save_usage_checkpoint(
                    task_id, {"tokens_used": self.usage.tokens_used, "calls": self.usage.calls}
                )

            self.usage.record_call = record_call
            saved_usage = await self.database.usage_checkpoint(task_id)
            self.usage.calls = max(self.usage.calls, saved_usage.get("calls", 0))
            self.usage.tokens_used = max(self.usage.tokens_used, saved_usage.get("tokens_used", 0))
            if not saved_usage:
                historical = await self.database.fetch_all(
                    "SELECT payload FROM event_log WHERE task_id=? AND event_type='budget.update' ORDER BY seq",
                    (task_id,),
                )
                previous = cumulative = 0
                for row in historical:
                    current = int(json.loads(row["payload"]).get("data", {}).get("calls", 0))
                    cumulative += current - previous if current >= previous else current
                    previous = current
                self.usage.calls = max(self.usage.calls, cumulative)
                await self.database.save_usage_checkpoint(
                    task_id,
                    {
                        "tokens_used": max(task.tokens_used, self.usage.tokens_used),
                        "calls": self.usage.calls,
                    },
                )
        for provider in getattr(self.search, "providers", ()):
            bind_task = getattr(provider, "bind_task", None)
            if callable(bind_task):
                await bind_task(task_id)
        if self.usage is not None and hasattr(self.usage, "tokens_used"):
            self.usage.tokens_used = max(self.usage.tokens_used, task.tokens_used)
        budget_row = await self.database.fetch_one(
            "SELECT payload FROM event_log WHERE task_id=? AND event_type='budget.update' ORDER BY seq DESC LIMIT 1",
            (task_id,),
        )
        if budget_row:
            prior = json.loads(budget_row["payload"]).get("data", {})
            self._search_calls = max(self._search_calls, int(prior.get("search_calls", 0)))
            self._fetch_calls = max(self._fetch_calls, int(prior.get("fetch_calls", 0)))
            if self.usage is not None and hasattr(self.usage, "calls"):
                self.usage.calls = max(self.usage.calls, int(prior.get("calls", 0)))
        self._budget_depth = task.depth
        token_limit = self.budget_for(task.depth).token_limit
        self._set_llm_phase_limit(token_limit, "final")

    async def run_task(self, task_id: str) -> None:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        await self._bind_task_runtime(task)
        token_limit = self.budget_for(task.depth).token_limit
        stop_requested = task.status == "stopping"
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
            and checkpoint
            and checkpoint.get("phase") == "topic_preflight"
            and task.resolved_event_query
        ):
            if not await self._preflight_manual_topic(task, task.resolved_event_query):
                await self._emit_budget(task_id, task.depth)
                return
            task = await self.database.get_task(task_id)
            assert task is not None
            checkpoint = await self.database.latest_checkpoint(task_id)
        if (
            not stop_requested
            and task.request_kind == "topic_discovery"
            and not task.resolved_event_query
        ):
            if checkpoint and checkpoint.get("phase") == "topic_discovery":
                await self._prepare_topic_candidates(
                    task,
                    date_from=str(checkpoint.get("date_from") or "") or None,
                    date_to=str(checkpoint.get("date_to") or "") or None,
                )
            elif not checkpoint or checkpoint.get("phase") != "topic_selection":
                await self._prepare_topic_candidates(task)
            else:
                await self.database.set_task_status(task_id, "paused", "topic_selection")
            await self._emit_budget(task_id, task.depth)
            return
        phase = checkpoint.get("phase") if checkpoint else None
        if phase == "outer" and task.resolved_event_query:
            await self._seed_selected_sources(task)
            checkpoint = await self.database.latest_checkpoint(task_id)
            phase = checkpoint.get("phase") if checkpoint else None
        if phase != "verified" and not stop_requested:
            investigation_limit = self._set_llm_phase_limit(token_limit, "investigation")
        else:
            investigation_limit = token_limit * 3 // 5
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
        if task.investigation_scope == "institution":
            investigation_query += f"\n【任务强制范围】{SCOPE_INSTRUCTION}"
        elif task.investigation_scope == "public_event":
            investigation_query += f"\n【公开事件隐私边界】{PUBLIC_EVENT_INSTRUCTION}"
        investigation_query += (
            f"\n信源范围：{task.source_scope}；检索语言：{', '.join(task.source_languages)}。"
            "最终报告使用中文；外文原文不可被译文替换。"
        )
        if not stop_requested and phase not in {"comments_ready", "verified"}:
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
            no_gain_rounds = int((checkpoint or {}).get("no_gain_rounds", 0))
            search_phase = "primary"
            for name in self.agents:
                if name not in active_agents:
                    await self.events.emit(
                        task_id,
                        "agent.status",
                        {"agent": name, "phase": "skipped", "inner_round": 0},
                    )
            for outer_round in range(start_round, effective_outer_rounds + 1):
                await self.database.set_outer_round(task_id, outer_round)
                before_items = await self.database.list_claims(task_id)
                before_claims = (len(before_items), sum(len(c.evidence_ids) for c in before_items))
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
                after_items = await self.database.list_claims(task_id)
                after_claims = (len(after_items), sum(len(c.evidence_ids) for c in after_items))
                no_gain_rounds = no_gain_rounds + 1 if after_claims <= before_claims else 0
                budget_exhausted = await self._emit_budget(task_id, task.depth)
                investigation_reserved = (
                    not budget_exhausted
                    and self.usage is not None
                    and int(getattr(self.usage, "tokens_used", 0))
                    >= investigation_limit - min(30_000, investigation_limit // 10)
                )
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
                    stop_requested = True
                    self._limitations.append(
                        "用户提前停止调查，报告仅基于停止前已封存的证据与陈述。"
                    )
                    await self.database.save_checkpoint(
                        task_id, "outer:stopped", {"phase": "investigated"}
                    )
                    break
                saturated = no_gain_rounds >= 2
                review = await self._moderate(
                    task_id,
                    investigation_query,
                    board,
                    outer_round,
                    allow_next_round=(
                        outer_round < effective_outer_rounds
                        and not budget_exhausted
                        and not investigation_reserved
                        and not saturated
                    ),
                )
                high_gaps = [gap.desc for gap in review.gaps if gap.priority == "high"]
                release = review.release and not high_gaps
                forced = (
                    outer_round >= effective_outer_rounds
                    or budget_exhausted
                    or investigation_reserved
                    or saturated
                )
                if release or forced:
                    end_reason = (
                        "approved"
                        if release
                        else "budget_exhausted"
                        if budget_exhausted
                        else "verification_reserve"
                        if investigation_reserved
                        else "no_progress"
                        if saturated
                        else "round_limit"
                    )
                    outcome = {
                        "phase": "investigated",
                        "end_reason": end_reason,
                        "approved": release,
                        "round": outer_round,
                    }
                    await self.database.save_checkpoint(task_id, "investigation:outcome", outcome)
                    if forced and not release:
                        unresolved = review.unresolved_critical + high_gaps
                        self._limitations.extend(
                            [f"讨论结束时仍未解决：{item}" for item in unresolved]
                            or ["达到协作轮次上限，仍需专项补查。"]
                        )
                    if budget_exhausted:
                        self._limitations.append(
                            "全局 token 预算已耗尽，调查结束并基于现有证据出报告。"
                        )
                    elif investigation_reserved:
                        self._limitations.append(
                            "主调查达到阶段预算上限，剩余 token 留给独立核验与报告；"
                            "未解决的缺口仍在报告中披露。"
                        )
                    await self.events.emit(
                        task_id,
                        "loop.round",
                        {
                            "scope": "outer",
                            "round": outer_round,
                            "decision": "release" if release else "force_release",
                            "end_reason": end_reason,
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
                    {
                        "phase": "outer",
                        "next_outer_round": outer_round + 1,
                        "no_gain_rounds": no_gain_rounds,
                    },
                )
            await self.database.save_checkpoint(
                task_id, "outer:investigated", {"phase": "investigated"}
            )

        checkpoint = await self.database.latest_checkpoint(task_id)
        phase = checkpoint.get("phase") if checkpoint else None
        if stop_requested:
            await self.database.save_checkpoint(
                task_id,
                "investigation:outcome",
                {"phase": "investigated", "end_reason": "user_stop", "approved": False},
            )
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
        # Comment analysis is an enrichment branch. It must start *after* the
        # first core report has passed its final scope/privacy review: both
        # branches use the same model endpoint and task budget, and running the
        # optional review first can starve the core release gate and downgrade a
        # usable report to an evidence brief.
        comment_analysis_task = None
        if checkpoint is None or checkpoint.get("phase") != "verified":
            self._set_llm_phase_limit(token_limit, "verification")
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
                    await self.events.emit(
                        task_id,
                        "verify.progress",
                        {
                            "claim_id": claim.local_id,
                            "badge": "unverified",
                            "verification_state": "skipped",
                            "total": len(claims),
                        },
                    )
                    continue
                verify_used += relation_count
                scheduled_claims.append(claim)

            for verified in await self._verify_claims(task_id, scheduled_claims, len(claims)):
                if verified.verification_state == "incomplete":
                    incomplete_claims += 1
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

        if not stop_requested:
            self._set_llm_phase_limit(token_limit, "verification")
            await self._corroborate_key_fact(task_id, task.depth)
        self._set_llm_phase_limit(token_limit, "final")
        await self.events.emit_task_status(
            task_id, status="running", phase="reporting", progress=88
        )
        if task.depth == "quick" and not stop_requested:
            await self._recover_report_gaps(task_id, investigation_query, board)
        current_task = await self.database.get_task(task_id)
        evidence = await self.database.list_evidence(task_id)
        main_ids = {
            item.local_id
            for item in evidence
            if (item.extra or {}).get("scope_status")
            in {"main", "foreign_supplement", "event_context"}
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
            and (claim.badge in {"verified", "disputed", "refuted"} or claim.verdict == "support")
            and any(evidence_id in main_ids for evidence_id in claim.evidence_ids)
        ]
        topic_selection = await self.database.checkpoint(task_id, "topic:selected") or {}
        forced_unverified_topic = bool(topic_selection.get("forced_unverified"))
        diagnostic_only = not main_ids or not verified_claims or forced_unverified_topic
        if diagnostic_only:
            self._limitations.append(
                "手工事件未通过来源预检，用户选择以线索继续；本次只能生成检索诊断。"
                if forced_unverified_topic
                else "未通过报告发布门：缺少本事件可用证据或已完成核验的关键陈述；已停止昂贵的分章生成，仅输出检索诊断。"
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
        # A resume from the verified checkpoint creates a fresh orchestrator. Restore
        # material agent warnings so a successful retry cannot silently erase them
        # from the report's limitations.
        rate_limit_codes = {
            "AGENT_FAILED",
            "AGENT_BUDGET_RESERVED",
            "AGENT_PLAN_RATE_LIMITED",
            "AGENT_SUMMARY_RATE_LIMITED",
            "AGENT_REFLECTION_RATE_LIMITED",
            "AGENT_SUMMARY_INCOMPLETE",
            "SCOPE_REVIEW_PARTIAL",
        }
        for event in await self.events.history(task_id):
            if event.event != "warning" or event.data.get("code") not in rate_limit_codes:
                continue
            message = event.data.get("message")
            if isinstance(message, str) and message and message not in self._limitations:
                self._limitations.append(message)
        report_id, report, html_path = await self.reports.build(
            task_id,
            forum=board.history(),
            orchestration_limitations=self._limitations,
            diagnostic_only=diagnostic_only,
        )
        if phase == "comments_ready":
            # Publish the core report before starting optional comment work. The
            # client can read a complete fact/propagation/action report while the
            # comment branch is still running; its result is folded in below.
            # Publish the core report while the optional comment branch is still
            # running. A second build below folds completed comment findings into
            # the final report; both report.done events are safe because the
            # client always keeps the newest URL.
            await self.database.save_report(
                task_id, report_id, report, html_path, report["metrics"]
            )
            await asyncio.to_thread(
                Path(html_path).write_text, render_html(report), encoding="utf-8"
            )
            await self.events.emit(
                task_id,
                "report.done",
                {
                    "report_id": report_id,
                    "html_url": f"/api/reports/{report_id}/html?view=full",
                    "partial": True,
                    "comment_pending": True,
                },
            )
            await self.events.emit_task_status(
                task_id, status="running", phase="comment_analysis", progress=90
            )
            comment_analysis_task = asyncio.create_task(
                self._run_comment_insight(task_id, investigation_query, board)
            )
            try:
                await comment_analysis_task
            except Exception as exc:
                self._limitations.append(
                    f"评论增量分析未完成（{type(exc).__name__}），核心报告仍按事实证据生成。"
                )
            try:
                candidate_id, candidate_report, candidate_path = await self.reports.build(
                    task_id,
                    forum=board.history(),
                    orchestration_limitations=self._limitations,
                    diagnostic_only=diagnostic_only,
                )
            except Exception as exc:
                self._limitations.append(
                    f"评论增量报告未能重新构建（{type(exc).__name__}），保留核心报告。"
                )
            else:
                # Optional comments must never make a previously releasable core
                # report worse because their own privacy review is partial. Keep
                # the candidate only when the release gate is at least as good; a
                # better candidate still brings completed comment findings into IR.
                if self._quality_progress(candidate_report) >= self._quality_progress(report):
                    report_id, report, html_path = candidate_id, candidate_report, candidate_path
                else:
                    retained = self.reports.retain_comment_increment(report, candidate_report)
                    if retained is not None:
                        report = retained
                    self._limitations.append(
                        "核心章节重建未达到此前质量，保留已审核心及仍有效的评论增量。"
                    )
        # Evaluate the assembled, reviewed report before deciding what to recover.
        # Persist each attempt, including failed attempts, so resume never resets the bound.
        progress = await self.database.checkpoint(task_id, "report:quality_recovery") or {}
        recovery_round = int(progress.get("round", 0))
        no_gain = int(progress.get("no_gain", 0))
        outcome = await self.database.checkpoint(task_id, "investigation:outcome") or {}
        end_reason = outcome.get("end_reason", "round_limit")
        while not stop_requested and not forced_unverified_topic and task.depth != "quick":
            missing = report.get("quality", {}).get("release_gate_missing", [])
            if any(
                k in missing
                for k in (
                    "institution_scope_report_review",
                    "scope_review_incomplete",
                    "scope_content_rejected",
                )
            ):
                end_reason = "review_incomplete"
                self._limitations.append(
                    "范围或隐私审查尚未完成，保留已经通过审查的内容；该故障不触发新取证或整份报告重建。"
                )
                break
            if not missing:
                end_reason = "core_complete"
                break
            current = await self.database.get_task(task_id)
            if not current or current.status in {"stopping", "pausing", "paused", "failed"}:
                end_reason = "user_stop"
                break
            if await self._emit_budget(task_id, task.depth):
                end_reason = "budget_exhausted"
                break
            if self.usage is not None and int(getattr(self.usage, "tokens_used", 0)) >= min(
                token_limit - max(30_000, token_limit // 10),
                token_limit * 9 // 10 - 10_000,
            ):
                end_reason = "budget_exhausted"
                self._limitations.append(
                    "剩余额度不足以完成一次补查、核验和报告重建，保留当前已审报告。"
                )
                break
            if no_gain >= 2:
                end_reason = "no_progress"
                break

            async def material_state():
                return (
                    [
                        (e.local_id, e.content_sha256, e.fetch_status)
                        for e in await self.database.list_evidence(task_id)
                    ],
                    [
                        (c.local_id, c.text, c.verification_state, c.verdict, c.evidence_ids)
                        for c in await self.database.list_claims(task_id)
                    ],
                    await self.database.checkpoint(task_id, "comments:analysis"),
                )

            prior_material = await material_state()
            before = self._quality_progress(report)
            recovery_round += 1
            # Count an interrupted attempt as no gain until its report proves otherwise.
            progress = {
                "phase": "verified",
                "round": recovery_round,
                "no_gain": no_gain + 1,
                "missing": missing,
            }
            await self.database.save_checkpoint(task_id, "report:quality_recovery", progress)
            await self.events.emit(
                task_id,
                "loop.round",
                {
                    "scope": "quality_recovery",
                    "round": recovery_round,
                    "decision": "start",
                    "reason": "按已审报告缺口定向补查",
                    "missing": missing,
                },
            )
            self._set_llm_phase_limit(token_limit, "verification")
            try:
                await self._recover_report_gaps(
                    task_id,
                    investigation_query,
                    board,
                    recovery_round=recovery_round,
                    missing=missing,
                )
                if "verifiable_key_claim" in missing:
                    await self._corroborate_key_fact(task_id, task.depth)
            finally:
                self._set_llm_phase_limit(token_limit, "final")
            current = await self.database.get_task(task_id)
            if not current or current.status in {"stopping", "pausing", "paused", "failed"}:
                end_reason = "user_stop"
                break
            if task.comment_mode != "off":
                await self._run_comment_insight(task_id, investigation_query, board)
            if prior_material == await material_state():
                no_gain += 1
                progress.update(no_gain=no_gain)
                await self.database.save_checkpoint(task_id, "report:quality_recovery", progress)
                continue
            recovered_evidence = {
                e.local_id
                for e in await self.database.list_evidence(task_id)
                if (e.extra or {}).get("scope_status")
                in {"main", "foreign_supplement", "event_context"}
            }
            has_basis = any(
                c.verification_state == "complete"
                and (c.badge in {"verified", "disputed", "refuted"} or c.verdict == "support")
                and set(c.evidence_ids) & recovered_evidence
                for c in await self.database.list_claims(task_id)
            )
            report_id, report, html_path = await self.reports.build(
                task_id,
                forum=board.history(),
                orchestration_limitations=self._limitations,
                diagnostic_only=forced_unverified_topic or not has_basis,
            )
            after = self._quality_progress(report)
            no_gain = 0 if after > before else no_gain + 1
            progress.update(
                no_gain=no_gain, missing=report.get("quality", {}).get("release_gate_missing", [])
            )
            await self.database.save_checkpoint(task_id, "report:quality_recovery", progress)
        current = await self.database.get_task(task_id)
        if current and current.status in {"pausing", "paused"}:
            await self.events.emit_task_status(
                task_id, status="paused", phase="reporting", progress=88
            )
            return
        progress.update(phase="verified", end_reason=end_reason)
        await self.database.save_checkpoint(task_id, "report:quality_recovery", progress)
        report.setdefault("quality", {})["recovery"] = progress
        await asyncio.to_thread(Path(html_path).write_text, render_html(report), encoding="utf-8")
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
