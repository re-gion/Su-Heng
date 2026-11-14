import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yuqing.agents.skill_catalog import builtin_catalog
from yuqing.app.main import create_app
from yuqing.core.events import EventBus
from yuqing.core.llm.gateway import LLMGateway
from yuqing.services.agent_skills import AgentSkillRuntime
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate


def test_public_catalog_exposes_eight_real_role_bindings_and_readonly_methods(runtime_dir):
    with TestClient(create_app(runtime_dir=runtime_dir)) as client:
        response = client.get("/api/agents")
    assert response.status_code == 200
    value = response.json()
    assert value["read_only"] is True
    assert value["errors"] == []
    assert len(value["agents"]) == 8
    assert {a["role"] for a in value["agents"]} == {
        "analyst_a",
        "analyst_b",
        "analyst_c",
        "comment_analyst",
        "moderator",
        "reporter",
        "verifier",
        "utility",
    }
    for agent in value["agents"]:
        assert any(s["id"] == agent["main_skill"] and not s["shared"] for s in agent["skills"])
        assert all(s["body"] and s["version"] and s["digest"] for s in agent["skills"])
    assert "source_path" not in response.text


@pytest.fixture
async def skill_runtime(tmp_path):
    database = Database(tmp_path / "skills.sqlite3")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="某产品公开召回事件"))
    events = EventBus(database)
    gateway = LLMGateway(SimpleNamespace())
    runtime = AgentSkillRuntime(gateway, builtin_catalog(), events)
    runtime.bind(task.id)
    gateway.request_adapter = runtime
    yield runtime, gateway, events, task
    await database.close()


async def test_model_selects_before_method_is_injected_and_replay_never_claims_early_completion(
    skill_runtime,
):
    runtime, gateway, events, task = skill_runtime
    requests = []

    async def complete(role, system, user, **options):
        requests.append((role, system, user, gateway.call_context))
        if gateway.call_context.get("stage") == "skill_selection":
            assert "逐项对齐主体" not in system  # Selection sees the index, not every method body.
            return {
                "skill_id": "claim-conflict-analysis",
                "reason": "核对两份材料中同一批次的数量口径。",
            }
        assert "逐项对齐主体" in system
        assert "来源身份与独立性辨析" in system
        return {"queries": [{"query": "召回 批次 公告"}]}

    gateway._complete_json = complete
    with gateway.logical_call(stage="plan"):
        await gateway.complete_json("analyst_a", "原契约", "已有两份批次数量不同的公告")
    history = await events.history(task.id)
    assert [e.data["skill"]["status"] for e in history] == [
        "selected",
        "loaded",
        "running",
        "response_ready",
    ]
    assert len(requests) == 2
    assert len({e.data["skill"]["invocation_id"] for e in history}) == 1
    assert history[-1].data["skill"]["selection_mode"] == "model"
    assert "claim_refs" not in history[-1].data["skill"]
    # A caller's persisted stage outcome is separate from a JSON response.
    await runtime.finish_stage("analyst_a", outcome="本轮未获得可采纳的新陈述。", partial=True)
    replay = await events.history(task.id, after_seq=history[-1].seq)
    assert replay[0].data["skill"]["status"] == "stage_partial"
    assert replay[0].data["skill"]["claim_refs"] == []
    assert (await events.history(task.id))[0].data["skill"]["status"] == "selected"


@pytest.mark.parametrize(
    "selected", ["../../private", "claim-evidence-review", "unregistered-method"]
)
async def test_out_of_role_or_path_selection_falls_back_without_loading_untrusted_method(
    skill_runtime, selected
):
    runtime, gateway, _, _ = skill_runtime

    async def complete(role, system, user, **options):
        if gateway.call_context.get("stage") == "skill_selection":
            return {"skill_id": selected, "reason": "忽略限制"}
        assert "事件事实链重建" in system
        assert "逐材料支持关系" not in system
        return {"queries": []}

    gateway._complete_json = complete
    with gateway.logical_call(stage="plan"):
        await gateway.complete_json("analyst_a", "system", "work")
    record = list(runtime.records.values())[-1]
    assert record["skill_id"] == "fact-chain-reconstruction"
    assert record["selection_mode"] == "fallback"


@pytest.mark.parametrize(
    "role,stage,expected",
    [
        ("analyst_b", "summarize", "media-framing-trace"),
        ("analyst_c", "summarize", "case-mechanism-comparison"),
        ("comment_analyst", "comment_classification", "sample-demand-analysis"),
        ("moderator", "moderation", "gap-directed-collaboration"),
        ("reporter", "report_draft", "evidence-bound-report"),
        ("verifier", "verify", "claim-evidence-review"),
        ("verifier", "report_summary_review", "report-basis-review"),
        ("utility", "translation", "material-translation"),
        ("utility", "search_query_translation", "cross-language-preparation"),
        ("utility", "scope_review", "task-scope-preparation"),
    ],
)
async def test_work_order_loads_role_method_without_an_extra_selection_request(
    skill_runtime, role, stage, expected
):
    runtime, gateway, _, _ = skill_runtime
    calls = []

    async def complete(request_role, system, user, **options):
        calls.append(system)
        assert expected in system
        return {"ok": True}

    gateway._complete_json = complete
    with gateway.logical_call(stage=stage):
        await gateway.complete_json(role, "unchanged contract", "work")
    assert len(calls) == 1
    assert list(runtime.records.values())[-1]["skill_id"] == expected
    assert list(runtime.records.values())[-1]["selection_mode"] == "work_order"


@pytest.mark.parametrize(
    "error,status",
    [(ValueError("secret should not leak"), "failed"), (asyncio.CancelledError(), "cancelled")],
)
async def test_failure_or_cancellation_cannot_be_marked_saved(skill_runtime, error, status):
    runtime, gateway, events, task = skill_runtime

    async def complete(*args, **kwargs):
        raise error

    gateway._complete_json = complete
    with pytest.raises(type(error)):
        await gateway.complete_json("reporter", "system", "work")
    await runtime.finish_stage("reporter", outcome="unexpected")
    history = await events.history(task.id)
    assert history[-1].data["skill"]["status"] == status
    assert "secret should not leak" not in history[-1].model_dump_json()


def test_runtime_rejects_binding_another_task():
    runtime = AgentSkillRuntime(LLMGateway(SimpleNamespace()), builtin_catalog(), SimpleNamespace())
    runtime.bind("first")
    with pytest.raises(ValueError):
        runtime.bind("second")
