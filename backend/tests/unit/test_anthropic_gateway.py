import json

import httpx
import pytest

from yuqing.core.llm.factory import ANTHROPIC_API_VERSION, LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway


@pytest.mark.asyncio
async def test_anthropic_protocol_sends_messages_request_and_normalizes_response():
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-fixture",
                "content": [{"type": "text", "text": '{"reply":"OK"}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
        )

    factory = LLMClientFactory(
        {
            "LLM_UTILITY_API_KEY": "fixture-secret",
            "LLM_UTILITY_BASE_URL": "https://relay.example/anthropic/v1",
            "LLM_UTILITY_MODEL": "claude-sonnet-fixture",
            "LLM_UTILITY_PROTOCOL": "anthropic",
        },
        anthropic_transport=httpx.MockTransport(respond),
    )
    gateway = LLMGateway(factory)
    try:
        result = await gateway.complete_json(
            "utility",
            "Only JSON please.",
            'Return {"reply":"OK"}.',
            max_tokens=128,
        )
    finally:
        await factory.aclose()

    assert result == {"reply": "OK"}
    assert captured["url"] == "https://relay.example/anthropic/v1/messages"
    assert captured["headers"]["x-api-key"] == "fixture-secret"
    assert captured["headers"]["anthropic-version"] == ANTHROPIC_API_VERSION
    assert captured["body"]["model"] == "claude-sonnet-fixture"
    assert captured["body"]["system"].endswith("只输出一个有效的 JSON 对象。")
    assert captured["body"]["messages"] == [{"role": "user", "content": 'Return {"reply":"OK"}.'}]
    assert "response_format" not in captured["body"]
    assert "temperature" not in captured["body"]
    assert gateway.tokens_used == 7
