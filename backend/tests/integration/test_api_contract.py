from pathlib import Path

from fastapi.testclient import TestClient

from yuqing.app.main import create_app


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


def test_failed_background_job_emits_one_terminal_status_event(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=FailingOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "失败事件"}).json()["task_id"]
        stream = client.get(f"/api/tasks/{task_id}/events")

    assert stream.text.count('"status": "failed"') == 1
    assert "event: error" in stream.text
    assert "event: task.status" in stream.text
