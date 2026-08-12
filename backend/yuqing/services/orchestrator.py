from __future__ import annotations

from pathlib import Path

from yuqing.agents.runtime import InvestigationAgent
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchProvider
from yuqing.core.search.base import SearchParams, SearchProvider
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.report_builder import BriefReportBuilder
from yuqing.services.verifier import ClaimVerifierService, EvidenceVerifier
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate
from yuqing.storage.snapshots import SnapshotStore


class M0Orchestrator:
    def __init__(
        self,
        database: Database,
        events: EventBus,
        *,
        search: SearchProvider,
        fetcher: FetchProvider,
        snapshots: SnapshotStore,
        agent: InvestigationAgent,
        verifier: EvidenceVerifier,
        reports_dir: Path,
        max_inner_rounds: int = 2,
    ):
        self.database = database
        self.events = events
        self.search = search
        self.evidence = EvidenceStore(database, snapshots, fetcher)
        self.agent = agent
        self.verification = ClaimVerifierService(database, verifier)
        entailment_verifier = verifier if hasattr(verifier, "entails") else None
        self.reports = BriefReportBuilder(database, reports_dir, entailment_verifier)
        self.max_inner_rounds = max(1, max_inner_rounds)

    async def _investigate(self, task_id: str, event_query: str, *, top_k: int) -> None:
        checkpoint = await self.database.latest_checkpoint(task_id)
        claims = await self.database.list_claims(task_id)
        if checkpoint and checkpoint.get("phase") == "investigated":
            return
        if checkpoint and checkpoint.get("phase") == "investigating":
            inner_round = int(checkpoint.get("next_round", 1))
            queries = list(checkpoint.get("next_queries") or [])
        else:
            inner_round = 1
            await self.events.emit(
                task_id,
                "agent.status",
                {"agent": "fact_investigator", "phase": "planning", "inner_round": inner_round},
            )
            plan = await self.agent.plan(event_query)
            queries = plan.queries
            await self.database.save_checkpoint(
                task_id,
                "inner1:planned",
                {"phase": "investigating", "next_round": 1, "next_queries": queries},
            )

        while inner_round <= self.max_inner_rounds and queries:
            for query in queries:
                await self.events.emit(
                    task_id,
                    "agent.status",
                    {
                        "agent": "fact_investigator",
                        "phase": "searching",
                        "inner_round": inner_round,
                        "queries": [query],
                    },
                )
                results = await self.search.search(SearchParams(query=query, top_k=top_k))
                records = await self.evidence.add_search_results(task_id, query, results)
                await self.events.emit(
                    task_id,
                    "search.result",
                    {
                        "agent": "fact_investigator",
                        "provider": self.search.name,
                        "query": query,
                        "hits": len(results),
                        "degraded_from": None,
                    },
                )
                for record in records:
                    updated = await self.evidence.fetch_one(record)
                    await self.events.emit(
                        task_id,
                        "evidence.added",
                        {
                            "evidence_id": updated.local_id,
                            "title": updated.title,
                            "source_name": updated.source_name,
                            "source_tier": updated.source_tier,
                            "published_at": updated.published_at,
                        },
                    )

            evidence = await self.database.list_evidence(task_id)
            await self.events.emit(
                task_id,
                "agent.status",
                {
                    "agent": "fact_investigator",
                    "phase": "summarizing",
                    "inner_round": inner_round,
                },
            )
            generated = await self.agent.summarize(event_query, evidence)
            known_texts = {claim.text for claim in claims}
            for item in generated:
                if item.text in known_texts:
                    continue
                claim = await self.database.add_claim(
                    ClaimCreate(
                        task_id=task_id,
                        text=item.text,
                        statement_kind=item.statement_kind,
                        rumor_text=item.rumor_text,
                        correction_text=item.correction_text,
                        agent="fact_investigator",
                        round=inner_round,
                        evidence_ids=item.evidence_ids,
                    )
                )
                known_texts.add(claim.text)
                await self.events.emit(
                    task_id,
                    "claim.added",
                    {
                        "claim_id": claim.local_id,
                        "text": claim.text,
                        "evidence_ids": claim.evidence_ids,
                        "agent": claim.agent,
                    },
                )

            claims = await self.database.list_claims(task_id)
            reflection = await self.agent.reflect(event_query, claims)
            should_continue = (
                reflection.should_continue
                and bool(reflection.next_queries)
                and inner_round < self.max_inner_rounds
            )
            await self.events.emit(
                task_id,
                "loop.round",
                {
                    "scope": "inner",
                    "agent": "fact_investigator",
                    "round": inner_round,
                    "decision": "continue" if should_continue else "stop",
                    "reason": reflection.reason,
                },
            )
            if not should_continue:
                await self.database.save_checkpoint(
                    task_id,
                    f"inner{inner_round}:investigated",
                    {"phase": "investigated", "claim_ids": [item.local_id for item in claims]},
                )
                return

            queries = reflection.next_queries[:3]
            await self.database.save_checkpoint(
                task_id,
                f"inner{inner_round}:continue",
                {
                    "phase": "investigating",
                    "next_round": inner_round + 1,
                    "next_queries": queries,
                    "claim_ids": [item.local_id for item in claims],
                },
            )
            inner_round += 1

        await self.database.save_checkpoint(
            task_id,
            f"inner{inner_round}:investigated",
            {"phase": "investigated", "claim_ids": [item.local_id for item in claims]},
        )

    async def run_task(self, task_id: str, *, halt_after_checkpoint: str | None = None) -> None:
        task = await self.database.get_task(task_id)
        if task is None:
            raise ValueError("task not found")
        await self.database.set_task_status(task_id, "running", "planning")
        await self.events.emit(
            task_id, "task.status", {"status": "running", "phase": "planning", "progress": 0}
        )
        checkpoint = await self.database.latest_checkpoint(task_id)
        checkpoint_phase = checkpoint.get("phase") if checkpoint else None
        investigation_query = task.event_query
        if task.time_range_from or task.time_range_to:
            investigation_query += f"（调查时间范围：{task.time_range_from or '不限'} 至 {task.time_range_to or '不限'}）"
        if checkpoint_phase != "verified":
            await self._investigate(
                task_id,
                investigation_query,
                top_k={"quick": 5, "standard": 8, "deep": 10}[task.depth],
            )
        if halt_after_checkpoint == "investigated":
            return

        if checkpoint_phase != "verified":
            await self.database.set_task_status(task_id, "running", "verifying")
            for claim in await self.database.list_claims(task_id):
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
            await self.database.save_checkpoint(task_id, "verify:complete", {"phase": "verified"})
        if halt_after_checkpoint == "verified":
            return

        await self.database.set_task_status(task_id, "running", "reporting")
        report_id, report, html_path = await self.reports.build(task_id)
        await self.database.save_report(task_id, report_id, report, html_path, report["metrics"])
        await self.events.emit(
            task_id,
            "report.done",
            {
                "report_id": report_id,
                "html_url": f"/api/reports/{report_id}/html",
                "metrics": report["metrics"],
            },
        )
        await self.database.set_task_status(task_id, "done", "finished")
        await self.events.emit(
            task_id, "task.status", {"status": "done", "phase": "finished", "progress": 100}
        )

    async def resume_task(self, task_id: str) -> None:
        checkpoint = await self.database.latest_checkpoint(task_id)
        if checkpoint is None:
            raise ValueError("没有可用检查点")
        await self.run_task(task_id)
