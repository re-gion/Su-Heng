import json
from types import SimpleNamespace

import pytest

from yuqing.core.llm.gateway import LLMGateway


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


class StaticCompletions:
    def __init__(self, content: str):
        self.content = content

    async def create(self, **_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
        )


class FakeFactory:
    def __init__(self):
        self.completions = FlakyCompletions()
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))

    def config(self, _role):
        return SimpleNamespace(model="fixture", temperature=0)

    def get(self, _role):
        return self.client


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
