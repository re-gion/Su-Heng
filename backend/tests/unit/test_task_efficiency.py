import asyncio
import json
from types import SimpleNamespace

import pytest

from yuqing.core.llm.gateway import LLMGateway


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["scope", "comments", "report"])
async def test_cancelling_parallel_work_collects_both_inflight_calls(stage):
    from yuqing.agents.comment_analysis import OpenAICommentAgent
    from yuqing.agents.reporter import OpenAIReportAgent
    from yuqing.services.institution_scope import InstitutionScopeReviewer

    class Blocking:
        active = 0
        ready = asyncio.Event()

        async def complete_json(self, *args, **kwargs):
            self.active += 1
            if self.active == 2:
                self.ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active -= 1

    gateway = Blocking()
    if stage == "scope":
        work = InstitutionScopeReviewer(gateway).review(
            [f"公开材料{i}" for i in range(24)], kind="report_text"
        )
    elif stage == "comments":
        work = OpenAICommentAgent(gateway, "system").analyze(
            "事件",
            [
                {
                    "id": str(i),
                    "text": f"评论{i}",
                    "source_url": "https://example.org/p",
                    "platform": "test",
                    "evidence_ref": "E001",
                }
                for i in range(24)
            ],
        )
    else:
        work = OpenAIReportAgent(gateway, "system").enrich({"facts": []})
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(gateway.ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_report_chapters_use_two_slots_and_keep_success_when_one_fails():
    from yuqing.agents.reporter import OpenAIReportAgent

    class Reporter(OpenAIReportAgent):
        active = 0
        peak = 0

        async def _draft(self, context, section):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            if section == "05":
                raise ConnectionError("fixture")
            return {"analyses": [{"section": section, "title": section}]}

        async def _review(self, context, draft):
            return {**draft, "analysis_review": {"status": "complete"}}

    reporter = Reporter(SimpleNamespace(), "system")
    result = await reporter.enrich({"facts": [{"origin_agent": "history_insight"}]})
    assert reporter.peak == 2
    assert [item["section"] for item in result["analyses"]] == ["07", "04", "06"]
    assert result["analysis_review"]["chapters"]["05"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_final_scope_review_waits_for_cooldown_without_bypassing_breaker(monkeypatch):
    import hashlib

    from yuqing.core.llm import gateway as module
    from yuqing.services.institution_scope import InstitutionScopeReviewer

    clock = [100.0]
    delays = []

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    factory = Factory('{"items":[{"id":0,"allowed":true}]}')
    gateway = LLMGateway(factory)
    endpoint = ("https://test.example/v1", hashlib.sha256(b"test").hexdigest())
    gateway._endpoint_failures[endpoint] = (3, 130.0)
    decisions = await InstitutionScopeReviewer(gateway).review(["公开章节"], kind="report_text")
    assert delays == [30.0]
    assert decisions[0].allowed
    assert len(factory.requests) == 1


class Factory:
    def __init__(self, content='{"ok":true}', *, model="measured-reasoner"):
        self.content = content
        self.model = model
        self.requests = []
        self.release = None

    def config(self, role):
        return SimpleNamespace(
            model=self.model, temperature=0, api_key="test", base_url="https://test.example/v1"
        )

    def get(self, role):
        return SimpleNamespace(chat=SimpleNamespace(completions=self))

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.release is not None:
            await self.release.wait()
        return SimpleNamespace(
            usage=SimpleNamespace(
                total_tokens=10000,
                completion_tokens=9417,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=8782),
            ),
            choices=[
                SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=self.content))
            ],
        )


@pytest.mark.asyncio
async def test_new_gateway_uses_measured_reasoning_budget_without_cold_retry():
    factory = Factory()
    gateway = LLMGateway(factory)
    gateway.learn_output_budgets(
        [
            {
                "model": factory.model,
                "role": "analyst_b",
                "stage": "summarize",
                "status": "complete",
                "output_tokens": 9417,
                "reasoning_tokens": 8782,
            }
        ]
    )
    with gateway.context(stage="summarize"):
        await gateway.complete_json("analyst_b", "system", "user", max_tokens=2000)
    assert factory.requests[0]["max_tokens"] >= 11772
    assert len(factory.requests) == 1


@pytest.mark.asyncio
async def test_learned_headroom_stays_within_phase_reservation_and_model_stage():
    factory = Factory()
    gateway = LLMGateway(factory)
    gateway.learn_output_budgets(
        [
            {
                "model": factory.model,
                "role": "utility",
                "stage": "utility",
                "status": "complete",
                "output_tokens": 20000,
                "reasoning_tokens": 19000,
            }
        ]
    )
    gateway.token_limit = 10000
    await gateway.complete_json("utility", "system", "user", max_tokens=2000)
    assert 2000 <= factory.requests[0]["max_tokens"] < 10000
    assert gateway._tokens_reserved == 0
    gateway.token_limit = None
    with gateway.context(stage="different_operation"):
        await gateway.complete_json("utility", "system", "user", max_tokens=2000)
    assert factory.requests[1]["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_json_in_extra_prose_is_not_extracted_or_silently_accepted():
    factory = Factory('explanation before {"ok":true}')
    gateway = LLMGateway(factory)
    with pytest.raises(json.JSONDecodeError):
        await gateway.complete_json("utility", "system", "user")
    assert len(factory.requests) == 3


@pytest.mark.asyncio
async def test_recorded_9417_token_case_avoids_two_length_retries_with_same_result():
    class RecordedCase(Factory):
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            output = min(kwargs["max_tokens"], 9417)
            complete = output == 9417
            return SimpleNamespace(
                usage=SimpleNamespace(
                    total_tokens=output + 6373,
                    completion_tokens=output,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=min(8782, output)),
                ),
                choices=[
                    SimpleNamespace(
                        finish_reason="stop" if complete else "length",
                        message=SimpleNamespace(
                            content='{"claim":"unchanged","refs":["E001"]}' if complete else ""
                        ),
                    )
                ],
            )

    cold_factory, warm_factory = RecordedCase(), RecordedCase()
    cold, warm = LLMGateway(cold_factory), LLMGateway(warm_factory)
    warm.learn_output_budgets(
        [
            {
                "model": warm_factory.model,
                "role": "analyst_b",
                "stage": "summarize",
                "status": "complete",
                "output_tokens": 9417,
                "reasoning_tokens": 8782,
            }
        ]
    )
    with cold.context(stage="summarize"), warm.context(stage="summarize"):
        before = await cold.complete_json("analyst_b", "system", "user")
        after = await warm.complete_json("analyst_b", "system", "user")
    assert before == after
    assert [r["max_tokens"] for r in cold_factory.requests] == [2000, 8000, 32000]
    assert [r["max_tokens"] for r in warm_factory.requests] == [12288]
    assert warm.tokens_used < cold.tokens_used


@pytest.mark.asyncio
async def test_whole_json_fence_is_unwrapped_without_another_model_call():
    factory = Factory('```json\n{"quote":"keep ``` and braces } exactly"}\n```')
    gateway = LLMGateway(factory)
    assert await gateway.complete_json("utility", "system", "user") == {
        "quote": "keep ``` and braces } exactly"
    }
    assert len(factory.requests) == 1


@pytest.mark.asyncio
async def test_invalid_json_keeps_structure_and_records_only_safe_parser_coordinates():
    factory = Factory('{"secret":"private words",}')
    gateway = LLMGateway(factory)
    records = []

    async def record(value):
        records.append(value)

    gateway.record_call = record
    with pytest.raises(json.JSONDecodeError):
        await gateway.complete_json("utility", "system", "user")
    failed = [r for r in records if r["status"] == "failed"]
    assert len(factory.requests) == 3
    assert failed[0]["json_error"]["line"] == 1
    assert "private words" not in json.dumps(records)


@pytest.mark.asyncio
async def test_waiting_call_is_visible_before_it_acquires_shared_slot(monkeypatch):
    monkeypatch.setenv("YUQING_LLM_MAX_INFLIGHT", "1")
    factory = Factory()
    factory.release = asyncio.Event()
    gateways = [LLMGateway(factory), LLMGateway(factory)]
    records = []

    async def record(value):
        records.append(value)

    for gateway in gateways:
        gateway.record_call = record
    tasks = [asyncio.create_task(g.complete_json("utility", "system", "user")) for g in gateways]
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(factory.requests) == 1
        assert sum(r["status"] == "queued" for r in records) == 2
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        assert any(r["status"] == "cancelled" for r in records)
        assert gateways[1]._tokens_reserved == 0
    finally:
        factory.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
