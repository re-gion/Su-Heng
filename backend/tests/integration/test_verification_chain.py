"""端到端守护：分级 → 采编主体归并 → 决策表必须能产出"已证实"。

历史缺陷（2026-09-11 实测）：`source_tiers.yaml` 覆盖过窄，加上 `merge_stances`
把 `unknown`/`syndicated` 整体丢弃，三次真实任务的 `verified_rate` 恒为 0.0——
而全仓库没有任何测试断言过它大于 0，所以缺陷长期潜伏。
"""

import json
from pathlib import Path

import pytest

from yuqing.core.fetch.base import FetchResult
from yuqing.core.search.base import SearchResult
from yuqing.services.evidence_store import EvidenceStore
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.investigation_scope import InvestigationScope
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
async def test_provider_fulltext_is_reusable_but_not_promoted_to_direct_snapshot(runtime_dir: Path):
    database = Database(runtime_dir / "provider-fulltext.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="搜索服务全文"))
    provider_text = "这是搜索服务返回的正文。" * 80

    records = await _store_results(
        runtime_dir,
        database,
        task.id,
        [
            SearchResult(
                url="https://example.com/report",
                title="公开报告",
                snippet="公开报告摘要",
                provider="langsearch",
                content_text=provider_text,
                content_origin="provider_fulltext",
            )
        ],
    )

    record = records[0]
    assert record.content_text == provider_text
    assert record.fetch_status == "discovered"
    assert record.snapshot_path is None
    assert record.content_sha256 is None
    assert record.extra["content_origin"] == "provider_fulltext"
    assert record.extra["provider_text_chars"] == len(provider_text)
    assert EvidenceStore.has_usable_provider_text(record) is True

    row = await database.fetch_one(
        "SELECT extra FROM evidence WHERE task_id = ? AND local_id = ?",
        (task.id, record.local_id),
    )
    assert json.loads(row["extra"])["provider_text_sha256"]
    await database.close()


@pytest.mark.asyncio
async def test_pending_provider_fulltext_still_requires_direct_fetch_for_date_gate(
    runtime_dir: Path,
):
    database = Database(runtime_dir / "provider-fulltext-pending.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="武汉大学图书馆事件"))
    provider_text = "发布时间：2025年9月20日。武汉大学通报图书馆事件调查复核情况。" * 30

    records = await _store_results(
        runtime_dir,
        database,
        task.id,
        [
            SearchResult(
                url="https://china.caixin.com/2025-09-20/example.html",
                title="武大通报图书馆事件调查复核情况",
                snippet="2025年9月20日，武汉大学发布调查复核通报。",
                provider="exa",
                content_text=provider_text,
                content_origin="provider_fulltext",
                raw={
                    "_scope": {
                        "scope_status": "pending",
                        "scope_reasons": ["date_untrusted"],
                        "main_eligible": False,
                    }
                },
            )
        ],
    )

    record = records[0]
    assert EvidenceStore.has_usable_provider_text(record) is True
    assert EvidenceStore.needs_direct_fetch(record) is True
    await database.close()


@pytest.mark.asyncio
async def test_saved_page_publisher_date_promotes_pending_evidence_to_main(runtime_dir: Path):
    class PublisherPageFetcher:
        async def fetch(self, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                html=(
                    '<div class="bd_block"><span id="pubtime_baidu">'
                    '2025-09-20 10:12:49</span><span id="source_baidu">'
                    "来源：财新网</span></div>"
                ),
                content_text="据新华社消息，武汉大学通报图书馆事件调查复核情况。",
                content_type="text/html",
            )

    database = Database(runtime_dir / "publisher-page-date.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="武汉大学图书馆事件"))
    records = await _store_results(
        runtime_dir,
        database,
        task.id,
        [
            SearchResult(
                url="https://china.caixin.com/2025-09-20/example.html",
                title="武大通报图书馆事件调查复核情况",
                snippet="据新华社消息，武汉大学通报图书馆事件调查复核情况。",
                provider="exa",
                raw={"_scope": {"scope_status": "pending", "scope_reasons": ["date_unknown"]}},
            )
        ],
    )
    store = EvidenceStore(
        database, SnapshotStore(runtime_dir / "snapshots"), PublisherPageFetcher()
    )
    scope = InvestigationScope(
        event_query="武汉大学图书馆事件",
        languages=("zh",),
        source_scope="domestic",
        date_from="2023-01-01",
        date_to="2026-01-01",
    )

    fetched = await store.fetch_one(records[0], scope=scope, agent="media_propagation")

    assert fetched.fetch_status == "fetched"
    assert fetched.published_at == "2025-09-20T10:12:49"
    assert fetched.extra["date_provenance"] == "page_metadata"
    assert fetched.extra["scope_status"] == "main"
    await database.close()


@pytest.mark.asyncio
async def test_direct_fetch_retry_clears_source_credit_missing_from_current_page(runtime_dir: Path):
    class ChangingPageFetcher:
        calls = 0

        async def fetch(self, url: str) -> FetchResult:
            self.calls += 1
            return FetchResult(
                url=url,
                html=(
                    "<article>公开报道正文</article><p>来源：新华社</p>"
                    if self.calls == 1
                    else "<article>更新后的公开报道正文</article>"
                ),
                content_text="公开报道正文",
                content_type="text/html",
            )

    database = Database(runtime_dir / "source-credit-retry.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="公开报道"))
        records = await _store_results(
            runtime_dir,
            database,
            task.id,
            [
                SearchResult(
                    url="https://example.com/report",
                    title="公开报道",
                    snippet="公开报道摘要",
                    provider="fixture",
                )
            ],
        )
        store = EvidenceStore(
            database, SnapshotStore(runtime_dir / "snapshots"), ChangingPageFetcher()
        )
        first = await store.fetch_one(records[0])
        assert first.extra["page_source_credits"] == ["来源：新华社"]
        await database.update_evidence_failed(task.id, first.local_id, "重新排队抓取")
        retry = await database.get_evidence(task.id, first.local_id)
        assert retry is not None

        refreshed = await store.fetch_one(retry)

        assert refreshed.fetch_status == "fetched"
        assert refreshed.extra["page_source_credits"] == []
        assert refreshed.extra["content_origin"] == "direct_fetch"
        stored = await database.get_evidence(task.id, first.local_id)
        assert stored is not None
        assert stored.extra["page_source_credits"] == []
    finally:
        await database.close()


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
async def test_retry_incomplete_claim_reuses_successful_evidence_relation(
    runtime_dir, claim_limits
):
    database = Database(runtime_dir / "retry-relations.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="机构公开报道"))
        records = await _store_results(
            runtime_dir,
            database,
            task.id,
            [
                SearchResult(
                    url=url, title="机构报道", snippet="机构已公开调查结果。", provider="fixture"
                )
                for url in ("https://hb.news.cn/news/a", "https://www.cnr.cn/news/b")
            ],
        )
        claim = await database.add_claim(
            ClaimCreate(
                task_id=task.id,
                text="机构已公开调查结果。",
                agent="fact_investigator",
                evidence_ids=[r.local_id for r in records],
            ),
            **claim_limits,
        )

        class OnceFailedVerifier:
            model_name = "fixture"
            calls = []
            failed = False

            async def verify(self, claim, evidence):
                self.calls.append(evidence.local_id)
                if evidence.local_id == records[1].local_id and not self.failed:
                    self.failed = True
                    raise ConnectionError("upstream unavailable")
                return VerificationRelation(
                    relation="support", reason="原文支持", cited_sentence=evidence.snippet
                )

        verifier = OnceFailedVerifier()
        service = ClaimVerifierService(database, verifier)
        incomplete = await service.verify_claim(claim)
        assert incomplete.verification_state == "incomplete"
        restored = await service.verify_claim(incomplete, reuse_completed=True)
        assert restored.verification_state == "complete"
        assert restored.badge == "verified"
        assert verifier.calls == [records[0].local_id, records[1].local_id, records[1].local_id]
    finally:
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
