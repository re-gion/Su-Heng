from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.core.search.base import SearchParams
from yuqing.services.provider_quota import ProviderQuotaManager


class IdleOrchestrator:
    async def run_task(self, task_id):
        return None

    async def resume_task(self, task_id):
        return None


def test_config_api_masks_secrets_supports_partial_override_and_clear(
    runtime_dir: Path, monkeypatch
):
    monkeypatch.setenv("DEFAULT_API_KEY", "sk-default-secret")
    monkeypatch.setenv("DEFAULT_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("DEFAULT_MODEL", "default-model")
    monkeypatch.setenv("LANGSEARCH_API_KEY", "ls-secret-123")
    monkeypatch.delenv("LLM_VERIFIER_MODEL", raising=False)
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:
        initial = client.get("/api/config")
        changed = client.put(
            "/api/config",
            json={"llm": {"roles": {"verifier": {"model": "verify-model"}}}},
        )
        current = client.get("/api/config")
        cleared = client.put("/api/config", json={"llm": {"roles": {"verifier": {"model": None}}}})

    assert initial.status_code == 200
    assert "default-secret" not in initial.text
    assert initial.json()["llm"]["default"]["api_key"].endswith("ret")
    assert changed.status_code == 200
    assert current.json()["llm"]["roles"]["verifier"]["effective"]["model"] == "verify-model"
    assert current.json()["llm"]["roles"]["verifier"]["source"]["model"] == "config"
    assert cleared.status_code == 200
    assert cleared.json()["llm"]["roles"]["verifier"]["effective"]["model"] == "default-model"


def test_config_api_exposes_domestic_foreign_search_and_fetch_order(runtime_dir: Path, monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER_ORDER", "langsearch,qianfan,bocha,exa,tavily,serper")
    monkeypatch.setenv("LANGSEARCH_API_KEY", "langsearch-env-secret")
    monkeypatch.setenv("QIANFAN_API_KEY", "qianfan-env-secret")
    monkeypatch.setenv("BOCHA_API_KEY", "bocha-env-secret")
    monkeypatch.setenv("EXA_API_KEY", "exa-env-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-env-secret")
    monkeypatch.setenv("SERPER_API_KEY", "serper-env-secret")
    monkeypatch.setenv("FETCH_PROVIDER_ORDER", "builtin,firecrawl")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-env-secret")
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())

    with TestClient(app) as client:
        today = datetime.now().astimezone().date().isoformat()
        client.portal.call(
            client.app.state.database.record_provider_usage,
            "search:qianfan:calls",
            f"day:{today}",
            40,
        )
        month = datetime.now().astimezone().strftime("month:%Y-%m")
        client.portal.call(
            client.app.state.database.record_provider_usage,
            "search:exa:milli_usd",
            month,
            20000,
        )
        client.portal.call(
            ProviderQuotaManager(client.app.state.database).reserve,
            "exa",
            SearchParams(query="test", top_k=5),
        )
        initial = client.get("/api/config")
        updated = client.put(
            "/api/config",
            json={
                "search": {
                    "keys": {"bocha": "bocha-config-secret", "exa": "exa-config-secret"},
                },
                "fetch": {
                    "provider_order": ["builtin", "firecrawl"],
                    "keys": {"firecrawl": "firecrawl-config-secret"},
                },
            },
        )

    assert initial.status_code == 200
    initial_payload = initial.json()
    assert initial_payload["search"]["provider_order"] == [
        "langsearch",
        "exa",
        "qianfan",
        "bocha",
        "tavily",
        "serper",
    ]
    assert set(initial_payload["search"]["keys"]) >= {
        "langsearch",
        "qianfan",
        "bocha",
        "exa",
        "tavily",
        "serper",
    }
    assert initial_payload["search"]["keys"]["bocha"] is not None
    assert initial_payload["search"]["quota"]["qianfan"]["state"] == "normal_limit_reached"
    assert initial_payload["search"]["quota"]["qianfan"]["windows"][0]["used"] == 40
    assert initial_payload["search"]["quota"]["qianfan"]["upstream_quota_verified"] is False
    exa_quota = initial_payload["search"]["quota"]["exa"]
    assert exa_quota["state"] == "metered_without_fixed_limit"
    assert exa_quota["windows"][0]["normal_limit"] is None
    assert exa_quota["windows"][0]["critical_limit"] is None
    assert exa_quota["windows"][0]["unit"] == "milli_usd"
    assert exa_quota["windows"][0]["used"] == 20012
    assert initial_payload["fetch"]["provider_order"] == ["builtin", "firecrawl"]
    assert initial_payload["fetch"]["keys"]["firecrawl"] is not None
    assert "env-secret" not in initial.text

    assert updated.status_code == 200
    updated_payload = updated.json()
    assert updated_payload["search"]["keys"]["bocha"].endswith("ret")
    assert updated_payload["search"]["keys"]["exa"].endswith("ret")
    assert updated_payload["fetch"]["keys"]["firecrawl"].endswith("ret")
    assert "bocha-config-secret" not in updated.text
    assert "exa-config-secret" not in updated.text
    assert "firecrawl-config-secret" not in updated.text


def test_config_api_rejects_unknown_search_or_fetch_provider(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:
        search = client.put("/api/config", json={"search": {"provider_order": ["unknown"]}})
        empty_search = client.put("/api/config", json={"search": {"provider_order": []}})
        fetch = client.put("/api/config", json={"fetch": {"provider_order": ["jina", "builtin"]}})

    assert search.status_code == 422
    assert search.json()["error"]["code"] == "CONFIG_INVALID"
    assert empty_search.status_code == 422
    assert empty_search.json()["error"]["code"] == "CONFIG_INVALID"
    assert fetch.status_code == 422
    assert fetch.json()["error"]["code"] == "CONFIG_INVALID"


def test_config_api_uses_fixed_search_order_and_rejects_legacy_override(
    runtime_dir: Path, monkeypatch
):
    monkeypatch.setenv("SEARCH_PROVIDER_ORDER", "langsearch,qianfan,zhipu,tavily,serper")
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())

    with TestClient(app) as client:
        current = client.get("/api/config")
        reordered = client.put(
            "/api/config",
            json={"search": {"provider_order": ["serper", "langsearch"]}},
        )

    assert current.status_code == 200
    assert current.json()["search"]["provider_order"] == [
        "langsearch",
        "exa",
        "qianfan",
        "bocha",
        "tavily",
        "serper",
    ]
    assert reordered.status_code == 422
    assert reordered.json()["error"]["code"] == "CONFIG_INVALID"
    assert "固定" in reordered.json()["error"]["message"]


def test_config_api_rejects_mask_placeholder(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:
        response = client.put(
            "/api/config",
            json={"llm": {"default": {"api_key": "sk-***abc"}}},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


def test_config_update_is_atomic_and_rejects_wrong_nested_types(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:
        partial = client.put(
            "/api/config",
            json={
                "llm": {
                    "default": {
                        "model": "must-not-stick",
                        "base_url": "not-a-url",
                    }
                }
            },
        )
        wrong_type = client.put("/api/config", json={"llm": "invalid"})
        current = client.get("/api/config")

    assert partial.status_code == 422
    assert wrong_type.status_code == 422
    assert current.json()["llm"]["default"]["model"] != "must-not-stick"


def test_config_api_exposes_and_validates_the_depth_budget(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:
        initial = client.get("/api/config")
        saved = client.put(
            "/api/config",
            json={
                "budget": {"overrides": {"standard": {"max_claims": 80, "max_verify_calls": 200}}}
            },
        )
        current = client.get("/api/config")
        invalid = client.put(
            "/api/config", json={"budget": {"overrides": {"turbo": {"max_claims": 5}}}}
        )
        after_invalid = client.get("/api/config")
        reset = client.put("/api/config", json={"budget": {"overrides": {}}})

    assert initial.status_code == 200
    initial_budget = initial.json()["budget"]
    # 默认表原样暴露，覆盖为空且来源标注为默认值。
    assert initial_budget["source"] == "default"
    assert initial_budget["overrides"] == {}
    assert initial_budget["effective"] == initial_budget["defaults"]
    assert initial.json()["fetch"]["quota"]["firecrawl"]["windows"][0]["critical_limit"] == 1000
    assert set(initial_budget["depths"]) == {"quick", "standard", "deep"}

    assert saved.status_code == 200
    assert current.json()["budget"]["source"] == "config"
    assert current.json()["budget"]["effective"]["standard"]["max_claims"] == 80
    assert current.json()["budget"]["overrides"] == {
        "standard": {"max_claims": 80, "max_verify_calls": 200}
    }
    # 只覆盖两个字段，其余仍取默认值。
    assert (
        current.json()["budget"]["effective"]["standard"]["top_k"]
        == current.json()["budget"]["defaults"]["standard"]["top_k"]
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "CONFIG_INVALID"
    assert after_invalid.json()["budget"]["effective"]["standard"]["max_claims"] == 80

    assert reset.status_code == 200
    assert reset.json()["budget"]["overrides"] == {}
    assert reset.json()["budget"]["effective"] == reset.json()["budget"]["defaults"]
