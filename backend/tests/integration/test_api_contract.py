from pathlib import Path

from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.services.public_interest import PublicInterestDecision


class ImmediateOrchestrator:
    def __init__(self, database, events):
        self.database = database
        self.events = events

    async def run_task(self, task_id):
        await self.database.set_task_status(task_id, "done", "finished")
        await self.events.emit(
            task_id, "task.status", {"status": "done", "phase": "finished", "progress": 100}
        )

    async def resume_task(self, task_id):
        await self.run_task(task_id)


class FailingOrchestrator(ImmediateOrchestrator):
    async def run_task(self, task_id):
        raise RuntimeError("fixture failure")


class IdleOrchestrator(ImmediateOrchestrator):
    async def run_task(self, task_id):
        return None


class ClosingOrchestrator(ImmediateOrchestrator):
    closed = False

    async def aclose(self):
        type(self).closed = True


def test_create_list_detail_and_replay_done_task_without_duplicate_terminal_event(
    runtime_dir: Path,
):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={"event_query": "API 契约测试", "depth": "quick"})
        assert created.status_code == 202
        task_id = created.json()["task_id"]

        detail = client.get(f"/api/tasks/{task_id}")
        listing = client.get("/api/tasks")
        stream = client.get(f"/api/tasks/{task_id}/events")

        assert detail.status_code == 200
        assert detail.json()["status"] == "done"
        assert detail.json()["progress"]["max_outer_rounds"] == 1
        assert listing.json()["items"][0]["task_id"] == task_id
        assert stream.text.count('"status": "done"') == 1
        assert "id: 1" in stream.text
        assert "event: task.status" in stream.text


def test_unknown_task_uses_stable_error_envelope(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        response = client.get("/api/tasks/t_missing")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "TASK_NOT_FOUND",
            "message": "任务不存在。",
            "recoverable": False,
            "details": {"task_id": "t_missing"},
        }
    }

    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        stream_response = client.get("/api/tasks/t_missing/events")
    assert stream_response.status_code == 404
    assert stream_response.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_create_task_persists_nested_time_range_and_validation_uses_error_envelope(
    runtime_dir: Path,
):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "event_query": "时间范围契约测试",
                "time_range": {"from": "2026-08-01", "to": "2026-08-12"},
            },
        )
        task_id = created.json()["task_id"]
        row = client.app.state.database._read(
            "SELECT time_range_from,time_range_to FROM task WHERE id=?", (task_id,)
        )[0]
        invalid = client.post("/api/tasks", json={"event_query": ""})

    assert created.status_code == 202
    assert tuple(row) == ("2026-08-01", "2026-08-12")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


def test_task_detail_exposes_depth_specific_outer_round_limit(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        task_id = client.post(
            "/api/tasks", json={"event_query": "深度轮次", "depth": "deep"}
        ).json()["task_id"]
        detail = client.get(f"/api/tasks/{task_id}")

    assert detail.json()["progress"]["max_outer_rounds"] == 3


def test_failed_background_job_emits_one_terminal_status_event(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=FailingOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "失败事件"}).json()["task_id"]
        stream = client.get(f"/api/tasks/{task_id}/events")

    assert stream.text.count('"status": "failed"') == 1
    assert "event: error" in stream.text
    assert "event: task.status" in stream.text


def test_policy_gate_blocks_private_person_case_before_task_creation(runtime_dir: Path):
    async def block(_database, _query, _note):
        return PublicInterestDecision(
            allowed=False, reason="涉及可识别未成年人私人指控", category="minor_private_case"
        )

    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=ImmediateOrchestrator,
        policy_checker=block,
    )
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"event_query": "某中学生私人纠纷"})
        tasks = client.get("/api/tasks").json()["items"]

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "POLICY_BLOCKED"
    assert tasks == []


def test_report_html_supports_brief_view_and_download(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "报告视图"}).json()["task_id"]
        ir = {
            "schema_version": "0.1",
            "min_reader_minor": 1,
            "task": {"task_id": task_id, "event_query": "报告视图"},
            "metrics": {},
            "blocks": [
                {
                    "block_id": "h",
                    "type": "report_header",
                    "section": "00",
                    "event_title": "报告视图",
                    "in_brief": True,
                },
                {
                    "block_id": "s",
                    "type": "text",
                    "section": "01",
                    "title": "速览内容",
                    "fallback_text": "速览正文",
                    "in_brief": True,
                },
                {
                    "block_id": "f",
                    "type": "text",
                    "section": "09",
                    "title": "完整内容",
                    "fallback_text": "仅完整版",
                    "in_brief": False,
                },
                {
                    "block_id": "e",
                    "type": "evidence_appendix",
                    "section": "09",
                    "items": [],
                    "in_brief": True,
                },
            ],
        }
        client.portal.call(
            client.app.state.database.save_report,
            task_id,
            "r_view",
            ir,
            str(runtime_dir / "unused.html"),
            {},
        )
        brief = client.get("/api/reports/r_view/html?view=brief")
        full = client.get("/api/reports/r_view/html?view=full&download=1")

    assert "速览正文" in brief.text and "仅完整版" not in brief.text
    assert "仅完整版" in full.text
    assert full.headers["content-disposition"].startswith("attachment;")


def test_background_job_closes_per_task_resources(runtime_dir: Path):
    ClosingOrchestrator.closed = False
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ClosingOrchestrator)
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"event_query": "资源释放"})

    assert response.status_code == 202
    assert ClosingOrchestrator.closed is True


def test_resume_claim_is_single_flight(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "并发续跑"}).json()["task_id"]
        assert client.portal is not None
        client.portal.call(
            client.app.state.database.save_checkpoint,
            task_id,
            "fixture:paused",
            {"phase": "outer", "next_outer_round": 1},
        )
        client.portal.call(client.app.state.database.set_task_status, task_id, "paused", "forum")
        first = client.post(f"/api/tasks/{task_id}/resume")
        second = client.post(f"/api/tasks/{task_id}/resume")

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TASK_NOT_RESUMABLE"


def test_pause_stop_and_delete_follow_task_state_contract(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "任务控制"}).json()["task_id"]
        assert client.portal is not None
        client.portal.call(client.app.state.database.set_task_status, task_id, "running", "forum")
        paused = client.post(f"/api/tasks/{task_id}/pause")
        blocked_delete = client.delete(f"/api/tasks/{task_id}")
        client.portal.call(client.app.state.database.set_task_status, task_id, "paused", "forum")
        stopped = client.post(f"/api/tasks/{task_id}/stop")
        client.portal.call(client.app.state.database.set_task_status, task_id, "done", "finished")
        deleted = client.delete(f"/api/tasks/{task_id}")

    assert paused.status_code == 202 and paused.json()["status"] == "pausing"
    assert blocked_delete.status_code == 409
    assert blocked_delete.json()["error"]["code"] == "TASK_NOT_DELETABLE"
    assert stopped.status_code == 202 and stopped.json()["will_generate_report"] is True
    assert deleted.status_code == 200
    assert deleted.json()["deleted"]["events"] >= 2
