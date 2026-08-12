import sqlite3

import pytest

from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate


@pytest.mark.asyncio
async def test_two_tasks_get_independent_local_ids_and_invalid_states_are_rejected(runtime_dir):
    database = Database(runtime_dir / "yuqing.db")
    await database.initialize()

    first = await database.create_task(TaskCreate(event_query="事件一", depth="quick"))
    second = await database.create_task(TaskCreate(event_query="事件二", depth="quick"))
    first_evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=first.id,
            url="https://example.com/a",
            title="来源一",
            snippet="公开摘要一",
            provider="fixture",
        )
    )
    second_evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=second.id,
            url="https://example.com/b",
            title="来源二",
            snippet="公开摘要二",
            provider="fixture",
        )
    )
    claim = await database.add_claim(
        ClaimCreate(
            task_id=first.id,
            text="事件一已有公开报道。",
            agent="fact_investigator",
            evidence_ids=[first_evidence.local_id],
        )
    )

    assert first_evidence.local_id == "E001"
    assert second_evidence.local_id == "E001"
    assert claim.local_id == "C001"
    assert (await database.get_claim(first.id, "C001")).evidence_ids == ["E001"]

    with pytest.raises(sqlite3.IntegrityError):
        await database.execute_write(
            """INSERT INTO evidence(
                   pk, task_id, local_id, url, url_hash, title, source_domain,
                   source_tier, discovered_at, fetch_status, snippet
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "bad",
                first.id,
                "E999",
                "https://bad.example",
                "bad",
                "bad",
                "bad.example",
                3,
                "2026-08-12T00:00:00+08:00",
                "fetched",
                "摘要",
            ),
        )

    with pytest.raises(ValueError, match="verbatim"):
        await database.add_claim(
            ClaimCreate(
                task_id=first.id,
                text="伪造逐字引述。",
                agent="fact_investigator",
                evidence_ids=[first_evidence.local_id],
                quotes=[
                    QuoteCreate(
                        evidence_id=first_evidence.local_id,
                        quote="摘要中并不存在的原文",
                        quote_type="verbatim",
                    )
                ],
            )
        )

    with pytest.raises(sqlite3.IntegrityError):
        await database.add_claim(
            ClaimCreate(
                task_id=first.id,
                text="fact 不得携带 correction_text。",
                statement_kind="fact",
                correction_text="非法更正",
                agent="fact_investigator",
                evidence_ids=[first_evidence.local_id],
            )
        )

    await database.close()
