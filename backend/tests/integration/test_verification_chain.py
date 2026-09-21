"""端到端守护：分级 → 采编主体归并 → 决策表必须能产出"已证实"。

历史缺陷（2026-09-11 实测）：`source_tiers.yaml` 覆盖过窄，加上 `merge_stances`
把 `unknown`/`syndicated` 整体丢弃，三次真实任务的 `verified_rate` 恒为 0.0——
而全仓库没有任何测试断言过它大于 0，所以缺陷长期潜伏。
"""

from pathlib import Path

import pytest

from yuqing.core.search.base import SearchResult
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.verifier import ClaimVerifierService, VerificationRelation
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, TaskCreate
from yuqing.storage.snapshots import SnapshotStore


class UnusedFetcher:
    name = "fixture"

    async def fetch(self, url: str):  # pragma: no cover - 本用例不触发原文抓取
        raise AssertionError("本用例不应触发原文抓取")


class AlwaysSupportVerifier:
    model_name = "fixture"

    async def verify(self, claim, evidence):
        return VerificationRelation(
            relation="support", reason="fixture", cited_sentence=evidence.snippet
        )


async def _store_results(runtime_dir: Path, database: Database, task_id: str, items):
    store = EvidenceStore(database, SnapshotStore(runtime_dir / "snapshots"), UnusedFetcher())
    return await store.add_search_results(task_id, "检索", items)


@pytest.mark.asyncio
async def test_two_distinct_outlets_verify_a_claim_and_lift_verified_rate(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "chain.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="多源互证"))
    records = await _store_results(
        runtime_dir,
        database,
        task.id,
        [
            SearchResult(
                url="https://hb.news.cn/news/a",
                title="新华社报道",
                snippet="新华社报道了该事件。",
                provider="fixture",
            ),
            SearchResult(
                url="https://www.cnr.cn/news/b",
                title="央广报道",
                snippet="央广报道了该事件。",
                provider="fixture",
            ),
        ],
    )

    # 分级与主体归并是这条链路的起点：两家都必须是可计数的独立采编主体。
    assert [record.source_role for record in records] == ["independent", "independent"]
    assert {record.publisher_entity for record in records} == {"新华社", "中央人民广播电台"}

    claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="该事件已有公开报道。",
            agent="fact_investigator",
            evidence_ids=[record.local_id for record in records],
        ),
        **claim_limits,
    )
    result = await ClaimVerifierService(database, AlwaysSupportVerifier()).verify_claim(claim)

    assert result.badge == "verified"
    assert result.independent_sources == 2
    assert result.verification_state == "complete"

    _, report, _ = await FullReportBuilder(database, runtime_dir / "reports").build(task.id)
    assert report["metrics"]["verified_rate"] > 0
    await database.close()


@pytest.mark.asyncio
async def test_portal_republication_alone_never_verifies_a_claim(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "chain-portals.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="门户转载"))
    records = await _store_results(
        runtime_dir,
        database,
        task.id,
        [
            SearchResult(
                url="https://www.sohu.com/a/1",
                title="搜狐转载",
                snippet="该事件确有发生。",
                provider="fixture",
            ),
            SearchResult(
                url="https://www.toutiao.com/a/2",
                title="头条转载",
                snippet="该事件确有发生。",
                provider="fixture",
            ),
            SearchResult(
                url="https://news.qq.com/a/3",
                title="腾讯转载",
                snippet="该事件确有发生。",
                provider="fixture",
            ),
        ],
    )

    # 三家门户转载同一篇通讯稿不能算"多源互证"：没有上游来源数据时，
    # 把门户计入独立信源就是伪造绿标（AGENTS.md 不变量 1）。
    assert {record.source_role for record in records} == {"syndicated"}
    claim = await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="该事件确有发生。",
            agent="fact_investigator",
            evidence_ids=[record.local_id for record in records],
        ),
        **claim_limits,
    )

    result = await ClaimVerifierService(database, AlwaysSupportVerifier()).verify_claim(claim)

    assert result.badge == "unverified"
    assert result.independent_sources == 0
    await database.close()
