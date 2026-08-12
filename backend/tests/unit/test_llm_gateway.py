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
