from pathlib import Path

from fastapi.testclient import TestClient

from yuqing.app.main import create_app


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
