import asyncio
from types import SimpleNamespace

import pytest

from yuqing.agents.openai_runtime import OpenAIInvestigationAgent
from yuqing.core.llm.gateway import LLMBudgetExhausted, LLMGateway
from yuqing.services.institution_scope import InstitutionScopeReviewer


class SequenceGateway:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_completed_summary_batch_survives_later_budget_failure():
    gateway = SequenceGateway(
        [
            {"claims": [{"text": "校方发布了公开通报。", "evidence_ids": ["E001"]}]},
            LLMBudgetExhausted("local phase budget"),
        ]
    )
    evidence = [
        SimpleNamespace(local_id=f"E{i:03}", title="通报", snippet="校方回应") for i in range(1, 6)
    ]
    agent = OpenAIInvestigationAgent(gateway, "system")
    saved = []

    async def persist(claims, fingerprint, evidence_ids):
        saved.extend(claims)

    claims = await agent.summarize("公共事件", evidence, on_batch=persist)

    assert [c.text for c in claims] == ["校方发布了公开通报。"]
    assert saved == claims
    assert agent.summary_incomplete["category"] == "local_budget"
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_scope_budget_failure_is_incomplete_and_stops_remaining_batches():
    gateway = SequenceGateway([LLMBudgetExhausted("local phase budget")])
    reviewer = InstitutionScopeReviewer(gateway)

    decisions = await reviewer.review(["校方公开回应。"] * 25, kind="claim")

    assert {d.status for d in decisions} == {"incomplete"}
    assert {d.reason for d in decisions} == {"local_budget"}
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_scope_connection_failure_stops_remaining_batches():
    reviewer = InstitutionScopeReviewer(SequenceGateway([ConnectionError("fixture unavailable")]))
    decisions = await reviewer.review(["校方公开回应。"] * 25, kind="claim")
    assert {d.status for d in decisions} == {"incomplete"}
    assert reviewer.gateway.calls == 1


@pytest.mark.asyncio
async def test_observed_reasoning_truncation_does_not_repeat_tiny_output_budget():
    sizes = []

    async def create(**kwargs):
        size = kwargs["max_tokens"]
        sizes.append(size)
        return SimpleNamespace(
            usage=SimpleNamespace(
                total_tokens=20, completion_tokens_details=SimpleNamespace(reasoning_tokens=10)
            ),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}'),
                    finish_reason="length" if size == 500 else "stop",
                )
            ],
        )

    factory = SimpleNamespace(
        config=lambda role: SimpleNamespace(
            model="fixture", temperature=0, base_url="https://example.org/v1", api_key="fixture"
        ),
        get=lambda role: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    gateway = LLMGateway(factory)
    await gateway.complete_json("verifier", "s", "u", max_tokens=500)
    await gateway.complete_json("verifier", "s", "u", max_tokens=500)
    assert sizes == [500, 2000, 2000]
    assert gateway.calls == 3


@pytest.mark.asyncio
async def test_endpoint_failure_does_not_fan_out_across_logical_calls():
    from yuqing.core.llm.gateway import LLMServiceUnavailable

    async def create(**kwargs):
        raise ConnectionError("fixture offline")

    factory = SimpleNamespace(
        config=lambda role: SimpleNamespace(
            model="fixture", temperature=0, base_url="https://example.org/v1", api_key="fixture"
        ),
        get=lambda role: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    gateway = LLMGateway(factory)
    with pytest.raises(ConnectionError):
        await gateway.complete_json("analyst_a", "s", "u")
    with pytest.raises(LLMServiceUnavailable):
        await gateway.complete_json("verifier", "s", "u")
    assert gateway.calls == 3


@pytest.mark.asyncio
async def test_incremental_summary_does_not_reprocess_material_when_batch_members_change():
    gateway = SequenceGateway(
        [
            {"claims": [{"text": "校方发布首份通报。", "evidence_ids": ["E001"]}]},
            {"claims": [{"text": "校方发布复核通报。", "evidence_ids": ["E002"]}]},
        ]
    )
    agent = OpenAIInvestigationAgent(gateway, "system")
    cache = {}

    async def load(key):
        return cache.get(key)

    async def save(key):
        cache[key] = {"complete": True}

    first = SimpleNamespace(local_id="E001", title="首份通报", snippet="首份公开回应")
    second = SimpleNamespace(local_id="E002", title="复核通报", snippet="后续复核结果")
    await agent.summarize("事件", [first], load_batch=load, save_processed=save)
    result = await agent.summarize("事件", [first, second], load_batch=load, save_processed=save)
    assert [c.evidence_ids for c in result] == [["E002"]]
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_saved_pending_batch_is_reviewed_before_generation_with_changed_inventory():
    gateway = SequenceGateway(
        [{"claims": [{"text": "校方公布后续复核。", "evidence_ids": ["E002"]}]}]
    )
    agent = OpenAIInvestigationAgent(gateway, "system")
    pending = {
        "claims": [{"text": "校方公布初次通报。", "evidence_ids": ["E001"]}],
        "evidence_ids": ["E001"],
    }
    reviewed = []

    async def pending_batches():
        return [("old-batch", pending)]

    async def review(claims, key, refs):
        reviewed.append(key)
        return False

    evidence = [
        SimpleNamespace(local_id=f"E00{i}", title="通报", snippet="公开回应") for i in (1, 2)
    ]
    result = await agent.summarize("事件", evidence, on_batch=review, list_pending=pending_batches)
    assert reviewed[0] == "old-batch"
    assert len(result) == 2
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_usage_checkpoint_is_monotonic_and_isolated_between_tasks(runtime_dir):
    from yuqing.storage.db import Database
    from yuqing.storage.models import TaskCreate

    db = Database(runtime_dir / "usage.db")
    await db.initialize()
    try:
        a = await db.create_task(TaskCreate(event_query="事件甲"))
        b = await db.create_task(TaskCreate(event_query="事件乙"))
        await asyncio.gather(
            db.save_usage_checkpoint(a.id, {"tokens_used": 500, "calls": 8}),
            db.save_usage_checkpoint(b.id, {"tokens_used": 200, "calls": 3}),
        )
        await db.save_usage_checkpoint(a.id, {"tokens_used": 100, "calls": 1})
        assert await db.usage_checkpoint(a.id) == {"tokens_used": 500, "calls": 8}
        assert await db.usage_checkpoint(b.id) == {"tokens_used": 200, "calls": 3}
        assert (await db.get_task(a.id)).tokens_used == 500
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_scope_check_migration_preserves_child_rows(runtime_dir):
    import sqlite3
    from pathlib import Path

    from yuqing.storage.db import Database
    from yuqing.storage.models import TaskCreate

    path = runtime_dir / "legacy.db"
    schema = (Path(__file__).parents[2] / "yuqing/storage/schema.sql").read_text(encoding="utf-8")
    schema = schema.replace("'general','institution','public_event'", "'general', 'institution'")
    with sqlite3.connect(path) as old:
        old.executescript(schema)
        old.execute(
            "INSERT INTO task(id,event_query,investigation_scope,status,created_at,updated_at) VALUES('old','旧事件','institution','done','2026-09-27','2026-09-27')"
        )
        old.execute("INSERT INTO analysis_batch VALUES('old','fact','f','{}')")
        old.execute("INSERT INTO scope_review VALUES('old','f','{}')")
    db = Database(path)
    await db.initialize()
    try:
        assert (await db.get_task("old")).investigation_scope == "institution"
        assert await db.get_analysis_batch("old", "fact", "f") == {}
        assert not await db.fetch_all("PRAGMA foreign_key_check")
        assert (
            await db.create_task(
                TaskCreate(event_query="新事件", investigation_scope="public_event")
            )
        ).investigation_scope == "public_event"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scope_explicit_rejection_is_distinct_from_invalid_output():
    reviewer = InstitutionScopeReviewer(
        SequenceGateway(
            [
                {"items": [{"id": 0, "allowed": False}]},
                {"items": []},
            ]
        )
    )
    rejected = await reviewer.review(["私人指控"], kind="claim")
    incomplete = await reviewer.review(["校方回应"], kind="claim")
    assert rejected[0].status == "rejected"
    assert incomplete[0].status == "incomplete"
    assert incomplete[0].reason == "invalid_output"


@pytest.mark.asyncio
async def test_gateway_retries_share_three_attempts_and_record_usage():
    class Completions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            # A truncation followed by invalid JSON must not reset the attempt bound.
            return SimpleNamespace(
                id="req-fixture",
                usage=SimpleNamespace(total_tokens=8, prompt_tokens=3, completion_tokens=5),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="invalid-json"),
                        finish_reason="length" if self.calls == 1 else "stop",
                    )
                ],
            )

    completions = Completions()
    factory = SimpleNamespace(
        config=lambda role: SimpleNamespace(
            model="fixture", temperature=0, base_url="https://example.org/v1", api_key="fixture"
        ),
        get=lambda role: SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    gateway = LLMGateway(factory)
    records = []

    async def record(value):
        records.append(value)

    gateway.record_call = record
    with pytest.raises(ValueError):
        await gateway.complete_json("analyst_a", "system", "user")
    assert completions.calls == 3
    assert gateway.tokens_used == 24
    records = [r for r in records if r["status"] not in {"queued", "inflight"}]
    assert [r["attempt"] for r in records] == [1, 2, 3]
    assert len({r["call_id"] for r in records}) == 1
    assert all(r["input_tokens"] == 3 and r["output_tokens"] == 5 for r in records)
    assert all(r["queue_ms"] >= 0 and r["request_ms"] >= 0 for r in records)


@pytest.mark.asyncio
async def test_concurrent_reservations_do_not_overspend_single_task_cap():
    started = asyncio.Event()
    release = asyncio.Event()

    async def create(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=20),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop"
                )
            ],
        )

    factory = SimpleNamespace(
        config=lambda role: SimpleNamespace(
            model="fixture", temperature=0, base_url="https://example.org/v1", api_key="fixture"
        ),
        get=lambda role: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    gateway = LLMGateway(factory)
    gateway.token_limit = 10000
    gateway.absolute_token_limit = 160
    first = asyncio.create_task(gateway.complete_json("analyst_a", "s", "u", max_tokens=100))
    await started.wait()
    with pytest.raises(LLMBudgetExhausted):
        await gateway.complete_json("analyst_b", "s", "u", max_tokens=100)
    release.set()
    assert await first == {"ok": True}
    assert gateway.calls == 1
    assert gateway._tokens_reserved == 0


@pytest.mark.asyncio
async def test_review_cache_survives_restart_and_policy_scope_does_not_share(runtime_dir):
    from yuqing.storage.db import Database
    from yuqing.storage.models import TaskCreate

    database = Database(runtime_dir / "review-cache.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(event_query="公开事件", investigation_scope="public_event")
        )
        gateway = SequenceGateway(
            [
                {
                    "items": [
                        {
                            "id": 0,
                            "allowed": True,
                            "redactions": [{"text": "张某", "replacement": "涉事学生"}],
                        }
                    ]
                }
            ]
        )
        reviewer = InstitutionScopeReviewer(gateway)
        reviewer.bind(database, task.id, "public_event")
        decision = (await reviewer.review(["校方撤销对张某的处分。"], kind="claim"))[0]
        assert decision.text == "校方撤销对涉事学生的处分。"
        restarted = InstitutionScopeReviewer(SequenceGateway([]))
        restarted.bind(database, task.id, "public_event")
        assert (await restarted.review([decision.text], kind="claim"))[0].allowed
        assert restarted.gateway.calls == 0
        assert (await restarted.review([decision.text], kind="report_text"))[0].allowed
        assert restarted.gateway.calls == 0
        restarted.bind(database, task.id, "institution")
        assert (await restarted.review([decision.text], kind="claim"))[0].status == "incomplete"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_optional_review_failure_preserves_authoritative_facts(runtime_dir):
    from yuqing.services.full_report import FullReportBuilder
    from yuqing.storage.db import Database
    from yuqing.storage.models import TaskCreate

    database = Database(runtime_dir / "block-retention.db")
    await database.initialize()
    try:
        task = await database.create_task(
            TaskCreate(event_query="公开事件", investigation_scope="public_event")
        )
        report = {
            "metrics": {"key_claims_rendered": 1},
            "blocks": [
                {
                    "block_id": "facts",
                    "type": "fact_check_table",
                    "items": [{"text": "校方公布复核结果。"}],
                },
                {
                    "block_id": "analysis",
                    "type": "analysis",
                    "items": [{"text": "还需要进一步分析。"}],
                },
            ],
        }
        builder = FullReportBuilder(
            database,
            runtime_dir / "reports",
            scope_reviewer=InstitutionScopeReviewer(SequenceGateway([LLMBudgetExhausted()])),
        )
        await builder._retain_reviewed_blocks(report, task)
        assert report["blocks"][0]["items"][0]["text"] == "校方公布复核结果。"
        assert report["quality"]["scope_review"]["rejected_texts"] == 0
        assert report["quality"]["scope_review"]["incomplete_texts"] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_summary_reuses_finished_batches_without_model_request():
    gateway = SequenceGateway([{"claims": [{"text": "校方公开回应。", "evidence_ids": ["E001"]}]}])
    agent = OpenAIInvestigationAgent(gateway, "system")
    caches = {}

    async def save(claims, fingerprint, evidence_ids):
        caches[fingerprint] = {"claims": [c.model_dump() for c in claims]}

    async def load(fingerprint):
        return caches.get(fingerprint)

    evidence = [SimpleNamespace(local_id="E001", title="通报", snippet="公开回应")]
    first = await agent.summarize("公共事件", evidence, on_batch=save, load_batch=load)
    second = await agent.summarize(
        "公共事件\n【已有陈述】校方公开回应", evidence, on_batch=save, load_batch=load
    )
    assert first == second
    assert gateway.calls == 1


def test_source_card_backlinks_only_target_rendered_bound_claims():
    from yuqing.render.validator import prune_citation_backlinks

    report = {
        "blocks": [
            {
                "type": "fact_check_table",
                "items": [{"claim_ref": "C002", "citations": [{"evidence_ref": "E001"}]}],
            },
            {
                "type": "evidence_appendix",
                "items": [
                    {
                        "evidence_ref": "E001",
                        "citations": [{"claim_ref": "C001"}, {"claim_ref": "C002"}],
                    },
                    {"evidence_ref": "E002", "citations": [{"claim_ref": "C002"}]},
                ],
            },
        ]
    }
    prune_citation_backlinks(report)
    cards = report["blocks"][1]["items"]
    assert cards[0]["citations"] == [{"claim_ref": "C002"}]
    assert cards[1]["citations"] == []
    assert report["blocks"][0]["items"][0]["claim_ref"] == "C002"
