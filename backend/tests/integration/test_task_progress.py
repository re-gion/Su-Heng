import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.core.events import EventBus
from yuqing.core.fetch.base import FetchResult
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.public_interest import PublicInterestDecision
from yuqing.services.v1_orchestrator import V1Orchestrator
from yuqing.services.verifier import ClaimVerifierService, VerificationRelation
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, TaskCreate
from yuqing.storage.snapshots import SnapshotStore


class ThreeEvents:
    def __init__(self, database, events):
        self.events = events

    async def run_task(self, task_id):
        await self.events.emit_task_status(task_id, status="running", phase="forum", progress=5)
        await self.events.emit(task_id, "verify.progress", {"claim_id": "C1", "total": 1})
        await self.events.emit_task_status(task_id, status="done", phase="finished", progress=100)


async def allow(_db, _query, _note):
    return PublicInterestDecision(allowed=True, reason="fixture", category="public_event")


def test_reconnect_prefers_last_event_id_and_progress_is_small(runtime_dir):
    with TestClient(
        create_app(runtime_dir=runtime_dir, orchestrator_factory=ThreeEvents, policy_checker=allow)
    ) as client:
        task_id = client.post("/api/tasks", json={"event_query": "机构公开通报"}).json()["task_id"]
        all_events = client.get(f"/api/tasks/{task_id}/events").text
        events = [
            json.loads(line[6:]) for line in all_events.splitlines() if line.startswith("data: ")
        ]
        last = events[-1]["seq"]
        response = client.get(
            f"/api/tasks/{task_id}/events?since_seq=0", headers={"Last-Event-ID": str(last - 1)}
        )
        replay = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert [e["seq"] for e in replay] == [last]
        snapshot = client.get(f"/api/tasks/{task_id}/progress").json()
        assert snapshot["seq"] == last
        assert snapshot["timing"]["status"] == "done"
        assert snapshot["timing"]["sampled_at"]
        assert "calls" not in snapshot["model_calls"]
        assert snapshot["model_calls"]["queued_requests"] == 0
        assert client.get("/api/tasks/missing/progress").status_code == 404


@pytest.mark.asyncio
async def test_fast_verification_is_persisted_and_published_while_slow_one_waits(runtime_dir):
    database = Database(runtime_dir / "progress.db")
    await database.initialize()
    gate = asyncio.Event()
    runner = None
    try:
        task = await database.create_task(TaskCreate(event_query="公开事件"))
        evidence = await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.org/source",
                title="来源",
                snippet="公开材料内容",
            )
        )
        claims = []
        for text in ("慢条目", "快条目"):
            claims.append(
                await database.add_claim(
                    ClaimCreate(
                        task_id=task.id,
                        text=text,
                        agent="fact_investigator",
                        round=1,
                        evidence_ids=[evidence.local_id],
                    ),
                    max_claims=10,
                    max_evidence_per_claim=4,
                )
            )

        class Verifier:
            model_name = "fixture"

            async def verify(self, claim, source):
                if claim.text == "慢条目":
                    await gate.wait()
                return VerificationRelation(
                    relation="support", reason="fixture", cited_sentence=source.snippet
                )

        orchestrator = V1Orchestrator.__new__(V1Orchestrator)
        orchestrator.events = EventBus(database)
        orchestrator.verification = ClaimVerifierService(database, Verifier())
        queue = orchestrator.events.subscribe(task.id)
        runner = asyncio.create_task(orchestrator._verify_claims(task.id, claims, 2))
        event = await asyncio.wait_for(queue.get(), 2)
        assert event.data["claim_id"] == claims[1].local_id
        assert event.data["total"] == 2
        assert not runner.done()
        persisted = await database.get_claim(task.id, claims[1].local_id)
        assert persisted.verification_state == "complete"
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        gate.set()
        await asyncio.sleep(0)
        assert queue.empty()  # cancelled sibling must not write after the parent stops
    finally:
        gate.set()
        if runner and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_parallel_seats_fetch_same_material_once_and_charge_once(runtime_dir):
    database = Database(runtime_dir / "dedup.db")
    await database.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    workers = []

    class Fetcher:
        name = "fixture"
        calls = 0

        async def fetch(self, url):
            self.calls += 1
            entered.set()
            await release.wait()
            return FetchResult(
                url=url, html="<p>公开内容</p>", content_text="公开内容", content_type="text/html"
            )

    try:
        task = await database.create_task(TaskCreate(event_query="公开事件"))
        evidence = await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.org/shared",
                title="共同来源",
                snippet="公开摘要",
            )
        )
        orchestrator = V1Orchestrator.__new__(V1Orchestrator)
        orchestrator.database = database
        orchestrator._fetch_locks = {}
        fetcher = Fetcher()
        orchestrator.evidence = EvidenceStore(
            database, SnapshotStore(runtime_dir / "snapshots"), fetcher
        )
        charges = []

        async def reserve(kind):
            charges.append(kind)
            return True

        orchestrator._reserve_tool = reserve
        for agent in ("fact_investigator", "media_propagation"):
            workers.append(
                asyncio.create_task(
                    orchestrator._fetch_once(evidence, scope=None, agent=agent, phase="primary")
                )
            )
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*workers)
        assert fetcher.calls == 1
        assert charges == ["fetch"]
        assert all(r.fetch_status == "fetched" for r in results)
        assert results[0].snapshot_path == results[1].snapshot_path
    finally:
        release.set()
        await asyncio.gather(*workers, return_exceptions=True)
        await database.close()
