import argparse
import json

import httpx
import pytest
from openai import AsyncOpenAI, AuthenticationError

from yuqing.core.llm import factory as factory_module
from yuqing.core.llm.roles import ROLES
from yuqing.scripts import llm_ping
from yuqing.storage.db import Database


async def store_config(runtime_dir, values):
    database = Database(runtime_dir / "yuqing.db")
    await database.initialize()
    try:
        await database.set_config_values(values, secret_keys={"DEFAULT_API_KEY"})
    finally:
        await database.close()


def install_transport(monkeypatch, handler):
    clients = []

    def create_client(**kwargs):
        client = AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(factory_module, "AsyncOpenAI", create_client)
    return clients


@pytest.mark.parametrize("role", ROLES)
async def test_ping_uses_stored_siliconflow_config_for_every_role(
    runtime_dir, monkeypatch, capsys, role
):
    monkeypatch.setenv("DEFAULT_API_KEY", "old-provider-fixture-key")
    monkeypatch.setenv("DEFAULT_BASE_URL", "https://old-provider.example/v1")
    monkeypatch.setenv("DEFAULT_MODEL", "old-model")
    for field in ("API_KEY", "BASE_URL", "MODEL"):
        monkeypatch.delenv(f"LLM_{role.upper()}_{field}", raising=False)
    await store_config(
        runtime_dir,
        {
            "DEFAULT_API_KEY": "siliconflow-fixture-key",
            "DEFAULT_BASE_URL": "https://api.siliconflow.cn/v1",
            "DEFAULT_MODEL": "zai-org/GLM-5.3-Flash",
        },
    )
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://api.siliconflow.cn/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer siliconflow-fixture-key"
        body = json.loads(request.content)
        assert body["model"] == "zai-org/GLM-5.3-Flash"
        assert body["response_format"] == {"type": "json_object"}
        assert not {"thinking", "enable_thinking", "reasoning_effort", "tools"} & body.keys()
        return httpx.Response(
            200,
            json={
                "id": "fixture-response",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"reply":"OK"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
            },
        )

    clients = install_transport(monkeypatch, handler)
    await llm_ping.main(argparse.Namespace(role=role, data_dir=str(runtime_dir)))

    assert len(requests) == 1
    assert len(clients) == 1 and clients[0].is_closed()
    output = capsys.readouterr().out
    assert "reply=OK tokens=14 model=zai-org/GLM-5.3-Flash" in output
    assert "fixture-key" not in output
    database = Database(runtime_dir / "yuqing.db")
    await database.initialize()
    try:
        assert await database.fetch_all("SELECT id FROM task") == []
    finally:
        await database.close()


async def test_ping_honors_role_override_and_closes_client_on_authentication_failure(
    runtime_dir, monkeypatch
):
    await store_config(
        runtime_dir,
        {
            "DEFAULT_API_KEY": "default-fixture-key",
            "DEFAULT_BASE_URL": "https://api.siliconflow.cn/v1",
            "DEFAULT_MODEL": "zai-org/GLM-5.3-Flash",
            "LLM_VERIFIER_API_KEY": "verifier-fixture-key",
            "LLM_VERIFIER_BASE_URL": "https://verifier.example/v1",
            "LLM_VERIFIER_MODEL": "independent-model",
        },
    )
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://verifier.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer verifier-fixture-key"
        assert json.loads(request.content)["model"] == "independent-model"
        return httpx.Response(401, json={"error": {"message": "invalid fixture key"}})

    clients = install_transport(monkeypatch, handler)
    with pytest.raises(AuthenticationError):
        await llm_ping.main(argparse.Namespace(role="verifier", data_dir=str(runtime_dir)))
    assert len(requests) == 1
    assert len(clients) == 1 and clients[0].is_closed()
