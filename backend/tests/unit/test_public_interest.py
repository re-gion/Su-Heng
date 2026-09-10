import pytest
from pydantic import ValidationError

from yuqing.services.public_interest import assess_public_interest


class FlakyGateway:
    """首次返回包装在 answer 字段里的 JSON（真实推理模型观测到的行为），第二次返回合格结构。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete_json(self, _role, _system, user, *, max_tokens=2000):
        self.prompts.append(user)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class StubConfigService:
    def __init__(self, _database):
        pass

    async def resolved_environ(self):
        return {}


class StubFactory:
    def __init__(self, _environ):
        pass

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_policy_gate_retries_once_when_output_invalid(monkeypatch):
    gateway = FlakyGateway(
        [
            {"answer": '{"allowed": true, "reason": "政策事项", "category": "政策"}'},
            {"allowed": True, "reason": "政策事项", "category": "政策"},
        ]
    )
    monkeypatch.setattr("yuqing.services.public_interest.ConfigService", StubConfigService)
    monkeypatch.setattr("yuqing.services.public_interest.LLMGateway", lambda _f: gateway)
    monkeypatch.setattr("yuqing.services.public_interest.LLMClientFactory", StubFactory)

    decision = await assess_public_interest(None, "某市地铁调价政策", None)

    assert decision.allowed is True
    assert len(gateway.prompts) == 2
    assert "上一次输出不是合法 JSON 对象" in gateway.prompts[1]


@pytest.mark.asyncio
async def test_policy_gate_raises_after_second_invalid_output(monkeypatch):
    gateway = FlakyGateway(
        [
            {"answer": "not-a-decision"},
            {"allowed": "是", "reason": "", "category": ""},
        ]
    )
    monkeypatch.setattr("yuqing.services.public_interest.ConfigService", StubConfigService)
    monkeypatch.setattr("yuqing.services.public_interest.LLMGateway", lambda _f: gateway)
    monkeypatch.setattr("yuqing.services.public_interest.LLMClientFactory", StubFactory)

    with pytest.raises((ValueError, ValidationError)):
        await assess_public_interest(None, "某事件", None)

    assert len(gateway.prompts) == 2
