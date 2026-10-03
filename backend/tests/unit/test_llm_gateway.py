import asyncio
import json
from types import SimpleNamespace

import pytest
from httpx import Request, Response
from openai import InternalServerError, RateLimitError

from yuqing.core.llm.gateway import (
    LLMGateway,
    LLMOutputTruncated,
    rate_limit_diagnostic,
    sanitize_upstream_message,
)


class FlakyCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        content = "not-json" if self.calls == 1 else '{"reply":"OK"}'
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


class Gateway504Completions:
    """前 N-1 次返回带 HTML 错误页的 504，最后一次成功，模拟上游网关抖动。"""

    def __init__(self, failures: int = 2):
        self.failures = failures
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            response = Response(
                504,
                request=Request("POST", "https://relay.example/v1/chat/completions"),
                text="<html><head><title>504 Gateway Time-out</title></head></html>",
            )
            raise InternalServerError(
                f"Error code: 504 - {response.text}", response=response, body=response.text
            )
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=3),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"reply":"OK"}'), finish_reason="stop"
                )
            ],
        )


class AccountQuotaCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        response = Response(
            429,
            request=Request("POST", "https://relay.example/v1/chat/completions"),
            json={"error": {"code": "insufficient_quota", "message": "secret account detail"}},
        )
        raise RateLimitError("secret account detail", response=response, body=response.json())


class ConcurrentCompletions:
    def __init__(self):
        self.active = 0
        self.peak = 0

    async def create(self, **_kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.03)
            return SimpleNamespace(
                usage=SimpleNamespace(total_tokens=3),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"reply":"OK"}'),
                        finish_reason="stop",
                    )
                ],
            )
        finally:
            self.active -= 1


class StaticCompletions:
    def __init__(self, content: str):
        self.content = content

    async def create(self, **_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
        )


class TruncatedThenOkCompletions:
    """前 N-1 次响应模拟推理模型把预算耗尽（finish=length），最后一次返回完整 JSON。"""

    def __init__(self, min_calls: int = 2):
        self.min_calls = min_calls
        self.calls: list[int] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs["max_tokens"])
        if len(self.calls) < self.min_calls:
            content = ""
            finish = "length"
        else:
            content = '{"reply":"OK"}'
            finish = "stop"
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=3),
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)
            ],
        )


class AlwaysTruncatedCompletions:
    def __init__(self):
        self.calls: list[int] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs["max_tokens"])
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")],
        )


class FakeFactory:
    def __init__(self):
        self.completions = FlakyCompletions()
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))

    def config(self, _role):
        return SimpleNamespace(
            model="fixture",
            temperature=0,
            base_url="https://relay.example/v1",
            api_key="fixture-key",
        )

    def get(self, _role):
        return self.client


@pytest.mark.asyncio
async def test_reasoning_profile_is_explicit_and_output_floor_does_not_cross_profiles():
    class Capture:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(total_tokens=3),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"reply":"OK"}'), finish_reason="stop"
                    )
                ],
            )

    factory = FakeFactory()
    capture = Capture()
    factory.client.chat.completions = capture
    gateway = LLMGateway(factory)
    gateway.learn_output_budgets(
        [
            {
                "model": "fixture",
                "role": "verifier",
                "stage": "review",
                "reasoning_effort": "high",
                "status": "complete",
                "output_tokens": 10000,
                "reasoning_tokens": 9000,
            }
        ]
    )
    with gateway.context(stage="review"):
        await gateway.complete_json(
            "verifier",
            "system",
            "user",
            max_tokens=2048,
            reasoning_effort="low",
            temperature=1.0,
            top_p=0.95,
        )
        await gateway.complete_json(
            "verifier", "system", "user", max_tokens=2048, reasoning_effort="high"
        )
        await gateway.complete_json("verifier", "system", "user", max_tokens=2048)
    assert capture.calls[0]["reasoning_effort"] == "low"
    assert capture.calls[0]["temperature"] == 1.0 and capture.calls[0]["top_p"] == 0.95
    assert capture.calls[0]["max_tokens"] == 2048
    assert capture.calls[1]["reasoning_effort"] == "high" and capture.calls[1]["max_tokens"] > 10000
    assert "reasoning_effort" not in capture.calls[2]
    assert "top_p" not in capture.calls[2] and capture.calls[2]["temperature"] == 0
    assert capture.calls[2]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_invalid_json_response_is_retried_before_returning():
    factory = FakeFactory()
    gateway = LLMGateway(factory)

    result = await gateway.complete_json("analyst_a", "system", "user")

    assert result == {"reply": "OK"}
    assert factory.completions.calls == 2
    assert gateway.calls == 2
    assert gateway.tokens_used == 6


@pytest.mark.asyncio
async def test_json_response_repairs_lone_surrogate_before_returning():
    factory = FakeFactory()
    factory.client.chat.completions = StaticCompletions(
        '{"claims":[{"text":"媒体传播口径\\ud83d","evidence_ids":["E001"]}]}'
    )
    gateway = LLMGateway(factory)

    result = await gateway.complete_json("analyst_b", "system", "user")

    assert json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert result == {"claims": [{"text": "媒体传播口径�", "evidence_ids": ["E001"]}]}


@pytest.mark.asyncio
async def test_json_response_preserves_valid_non_bmp_characters():
    factory = FakeFactory()
    factory.client.chat.completions = StaticCompletions('{"reply":"传播趋势\\ud83d\\ude00"}')
    gateway = LLMGateway(factory)

    result = await gateway.complete_json("analyst_b", "system", "user")

    assert result == {"reply": "传播趋势😀"}


@pytest.mark.asyncio
async def test_token_budget_rejects_call_before_external_request():
    factory = FakeFactory()
    gateway = LLMGateway(factory)
    gateway.token_limit = 10

    with pytest.raises(RuntimeError, match="token budget exhausted"):
        await gateway.complete_json("analyst_a", "system", "user", max_tokens=100)

    assert factory.completions.calls == 0


@pytest.mark.asyncio
async def test_length_truncation_grows_max_tokens_and_retries_once():
    factory = FakeFactory()
    factory.client.chat.completions = TruncatedThenOkCompletions()
    gateway = LLMGateway(factory)

    result = await gateway.complete_json("analyst_a", "system", "user", max_tokens=300)

    assert result == {"reply": "OK"}
    assert factory.client.chat.completions.calls == [300, 1200]
    assert gateway.calls == 2
    assert gateway.tokens_used == 6


@pytest.mark.asyncio
async def test_length_retry_can_exceed_8192_but_remains_bounded():
    factory = FakeFactory()
    factory.client.chat.completions = AlwaysTruncatedCompletions()
    gateway = LLMGateway(factory)

    with pytest.raises(LLMOutputTruncated, match="长度上限"):
        await gateway.complete_json("analyst_a", "system", "user", max_tokens=6000)

    assert factory.client.chat.completions.calls == [6000, 24000, 32768]
    assert gateway.tokens_used == 9


@pytest.mark.asyncio
async def test_length_retry_cannot_exceed_remaining_total_budget():
    factory = FakeFactory()
    factory.client.chat.completions = TruncatedThenOkCompletions()
    gateway = LLMGateway(factory)
    gateway.token_limit = 500
    with pytest.raises(RuntimeError, match="token budget exhausted"):
        await gateway.complete_json("analyst_a", "system", "user", max_tokens=300)
    assert gateway.calls == 1
    assert gateway.tokens_used == 3
    assert factory.client.chat.completions.calls == [300]


@pytest.mark.asyncio
async def test_tiny_budget_grows_through_multiple_steps():
    factory = FakeFactory()
    factory.client.chat.completions = TruncatedThenOkCompletions(min_calls=3)
    gateway = LLMGateway(factory)

    result = await gateway.complete_json("analyst_a", "system", "user", max_tokens=32)

    assert result == {"reply": "OK"}
    assert factory.client.chat.completions.calls == [32, 128, 512]
    assert gateway.calls == 3
    assert gateway.tokens_used == 9


@pytest.mark.asyncio
async def test_upstream_504_is_retried_and_survives_transient_gateway_errors():
    factory = FakeFactory()
    factory.client.chat.completions = Gateway504Completions(failures=2)
    gateway = LLMGateway(factory)

    result = await gateway.complete_json("analyst_a", "system", "user")

    assert result == {"reply": "OK"}
    assert factory.client.chat.completions.calls == 3


@pytest.mark.asyncio
async def test_upstream_504_exhausting_retries_raises_sanitized_error():
    factory = FakeFactory()
    factory.client.chat.completions = Gateway504Completions(failures=99)
    gateway = LLMGateway(factory)

    with pytest.raises(InternalServerError) as exc_info:
        await gateway.complete_json("analyst_a", "system", "user")

    message = sanitize_upstream_message(exc_info.value)
    assert "504" in message
    assert "<html" not in message.lower()


def test_sanitize_keeps_non_upstream_exception_detail():
    class AppBug(RuntimeError):
        pass

    exc = AppBug("claim 预算状态错乱：pk=3")
    assert sanitize_upstream_message(exc) == "AppBug: claim 预算状态错乱：pk=3"


@pytest.mark.asyncio
async def test_account_quota_429_is_classified_without_retry_or_leaking_body():
    factory = FakeFactory()
    quota = AccountQuotaCompletions()
    factory.client.chat.completions = quota
    gateway = LLMGateway(factory)

    with pytest.raises(RateLimitError) as exc_info:
        await gateway.complete_json("analyst_b", "system", "user")

    assert quota.calls == 1
    assert rate_limit_diagnostic(exc_info.value)["category"] == "account_quota"
    message = sanitize_upstream_message(exc_info.value)
    assert "账户" in message
    assert "secret account detail" not in message


def test_429_distinguishes_concurrency_and_retry_after_without_raw_body():
    response = Response(
        429,
        headers={"Retry-After": "7"},
        request=Request("POST", "https://relay.example/v1/chat/completions"),
        json={"error": {"code": "concurrency_limit", "message": "key=private"}},
    )
    error = RateLimitError("key=private", response=response, body=response.json())

    assert rate_limit_diagnostic(error) == {
        "category": "concurrency",
        "retry_after_seconds": 7,
    }
    assert "并发" in sanitize_upstream_message(error)
    assert "private" not in sanitize_upstream_message(error)


def test_429_without_provider_detail_stays_unclassified():
    response = Response(
        429,
        request=Request("POST", "https://relay.example/v1/chat/completions"),
        text="<html>credential=private</html>",
    )
    error = RateLimitError("credential=private", response=response, body=response.text)

    assert rate_limit_diagnostic(error) == {"category": "unknown"}
    assert "原因未指明" in sanitize_upstream_message(error)
    assert "private" not in sanitize_upstream_message(error)


@pytest.mark.asyncio
async def test_gate_limits_inflight_calls_across_gateway_instances(monkeypatch):
    monkeypatch.setenv("YUQING_LLM_MAX_INFLIGHT", "2")
    factory = FakeFactory()
    concurrent = ConcurrentCompletions()
    factory.client.chat.completions = concurrent
    gateways = [LLMGateway(factory) for _ in range(3)]

    await asyncio.gather(
        *(gateway.complete_json("analyst_a", "system", "user") for gateway in gateways)
    )

    assert concurrent.peak == 2
