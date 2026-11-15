import pytest

from yuqing.services.full_report import FullReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["structural_rejected", "review_rejected"])
async def test_relation_checkpoint_version_and_incomplete_structure_are_not_reused(
    runtime_dir, claim_limits, category
):
    database = Database(runtime_dir / "relations.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="云海公司设备召回事件"))
        text = "云海公司2025年1月1日发布设备召回通报。"
        evidence = await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.cn/notice",
                title="云海公司设备召回通报",
                snippet=text,
                content_text=text,
                source_name="云海公司",
                source_role="party",
                source_tier=2,
                published_at="2025-01-01",
                fetch_status="fetched",
                fetched_at="2025-01-01T12:00:00+08:00",
                snapshot_path="fixture.html",
                content_sha256="fixture",
                extra={
                    "scope_status": "main",
                    "main_eligible": True,
                    "date_provenance": "page_visible",
                },
            )
        )
        claim = await database.add_claim(
            ClaimCreate(
                task_id=task.id,
                text=text,
                agent="fact_investigator",
                evidence_ids=[evidence.local_id],
                quotes=[
                    QuoteCreate(evidence_id=evidence.local_id, quote=text, quote_type="verbatim")
                ],
            ),
            **claim_limits,
        )
        await database.set_claim_verification(
            claim.pk,
            badge="verified",
            verdict="support",
            reason="fixture",
            state="complete",
            independent_sources=1,
            max_source_tier=2,
            verifier_model="fixture",
        )

        class Reporter:
            RELATION_VERSION = "source-identity-v3"
            calls = 0
            relation_candidates = []
            relation_diagnostics = [{"category": category}]

            async def enrich(self, context):
                return {}

            async def recover_relations(self, sources):
                self.calls += 1
                return []

        reporter = Reporter()
        builder = FullReportBuilder(database, runtime_dir / "reports", reporter=reporter)
        await builder.build(task.id)
        assert reporter.calls == 1
        checkpoint = await database.checkpoint(task.id, "report:relations")
        assert checkpoint["version"] == reporter.RELATION_VERSION
        assert checkpoint["status"] == (
            "partial" if category == "structural_rejected" else "complete"
        )
        await builder.build(task.id)
        assert reporter.calls == (2 if category == "structural_rejected" else 1)
        # A pre-fix empty result once marked complete must not suppress a fresh recovery.
        checkpoint.update(version=2, status="complete")
        await database.save_checkpoint(task.id, "report:relations", checkpoint)
        previous_calls = reporter.calls
        await builder.build(task.id)
        assert reporter.calls == previous_calls + 1
    finally:
        await database.close()
