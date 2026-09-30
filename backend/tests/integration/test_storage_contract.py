import sqlite3
from pathlib import Path

import pytest

from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate


@pytest.mark.asyncio
async def test_existing_task_table_gains_institution_scope_column(runtime_dir):
    path = runtime_dir / "legacy-scope.db"
    schema = (Path(__file__).parents[2] / "yuqing" / "storage" / "schema.sql").read_text(
        encoding="utf-8"
    )
    old_column = (
        "  investigation_scope TEXT NOT NULL DEFAULT 'general' "
        "CHECK (investigation_scope IN ('general','institution','public_event')),\n"
    )
    assert old_column in schema
    connection = sqlite3.connect(path)
    connection.executescript(schema.replace(old_column, ""))
    connection.close()

    database = Database(path)
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="机构公开回应", investigation_scope="institution")
    )
    assert task.investigation_scope == "institution"
    await database.close()


@pytest.mark.asyncio
async def test_two_tasks_get_independent_local_ids_and_invalid_states_are_rejected(
    runtime_dir, claim_limits
):
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
        ),
        **claim_limits,
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
            ),
            **claim_limits,
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
            ),
            **claim_limits,
        )

    await database.close()


@pytest.mark.asyncio
async def test_claim_limits_come_from_the_caller_and_are_enforced(runtime_dir):
    """上限由调用方注入（storage 读不到配置系统），注入值必须真正生效。"""
    database = Database(runtime_dir / "limits.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="预算上限", depth="quick"))
    evidence = [
        await database.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url=f"https://news.example.com/{index}",
                title=f"来源{index}",
                snippet=f"公开摘要{index}",
                provider="fixture",
            )
        )
        for index in range(3)
    ]

    with pytest.raises(ValueError, match="最多绑定 2 条证据"):
        await database.add_claim(
            ClaimCreate(
                task_id=task.id,
                text="绑定三条证据超出注入上限。",
                agent="fact_investigator",
                evidence_ids=[item.local_id for item in evidence],
            ),
            max_claims=10,
            max_evidence_per_claim=2,
        )

    await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="第一条陈述。",
            agent="fact_investigator",
            evidence_ids=[evidence[0].local_id],
        ),
        max_claims=1,
        max_evidence_per_claim=2,
    )
    with pytest.raises(ValueError, match="上限 1"):
        await database.add_claim(
            ClaimCreate(
                task_id=task.id,
                text="第二条陈述。",
                agent="fact_investigator",
                evidence_ids=[evidence[0].local_id],
            ),
            max_claims=1,
            max_evidence_per_claim=2,
        )
    await database.close()
