import json
import sqlite3

from fastapi.testclient import TestClient

from yuqing.app.main import create_app


class Runner:
    calls = []

    def __init__(self, database, events):
        self.database = database

    async def run_task(self, task_id):
        await self.database.set_task_status(task_id, "done", "finished")

    async def deepen_comment_question(self, task_id, question_id, *, follow_up=False):
        self.calls.append((task_id, question_id, follow_up))
        return {"status": "budget_limited", "message": "原任务剩余额度不足"}

    async def aclose(self):
        pass


def published(client, runtime_dir):
    task_id = client.post("/api/tasks", json={"event_query": "公共机构处置事件"}).json()["task_id"]
    block = {
        "type": "comment_insight",
        "analysis_version": 5,
        "analysis_mode": "quick_read",
        "samples": [],
        "items": [{"id": "Q123", "title": "复核程序是什么？", "review_status": "accepted"}],
        "observations": [],
    }
    with sqlite3.connect(runtime_dir / "yuqing.db") as connection:
        connection.execute(
            "INSERT INTO report(id,task_id,ir_json,html_path,metrics,generated_at) VALUES(?,?,?,?,?,?)",
            (
                "r_fixture",
                task_id,
                json.dumps({"blocks": [block]}),
                "unused.html",
                "{}",
                "2026-10-03T00:00:00Z",
            ),
        )
    return task_id


def test_deepening_is_explicit_and_busy_or_missing_requests_do_not_launch_calls(runtime_dir):
    Runner.calls = []
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=Runner, demo_mode=False)
    with TestClient(app) as client:
        task = published(client, runtime_dir)
        endpoint = f"/api/tasks/{task}/comment-questions/Q123/analyze"
        assert client.get(f"/api/tasks/{task}/comment-questions").json()["jobs"] == {}
        assert not Runner.calls
        app.state.comment_analysis_jobs.add(task)
        assert client.post(endpoint, json={}).status_code == 409
        assert client.delete(f"/api/tasks/{task}").status_code == 409
        app.state.comment_analysis_jobs.clear()
        assert client.post(endpoint.replace("Q123", "unknown"), json={}).status_code == 404
        assert client.post(endpoint, json={"mode": "invalid"}).status_code == 422
        assert not Runner.calls
        assert client.post(endpoint, json={}).status_code == 202
        assert Runner.calls == [(task, "Q123", False)]
        state = client.get(f"/api/tasks/{task}/comment-questions").json()["jobs"]["Q123"]
        assert state["status"] == "budget_limited"
        assert not app.state.comment_analysis_jobs
        assert client.post(endpoint, json={"mode": "follow_up"}).status_code == 202
        assert Runner.calls[-1] == (task, "Q123", True)
        assert client.get("/api/tasks/unknown/comment-questions").status_code == 404


def test_takedown_and_container_block_deepening(runtime_dir, monkeypatch):
    Runner.calls = []
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=Runner, demo_mode=False)
    with TestClient(app) as client:
        task = published(client, runtime_dir)
        assert (
            client.post(
                "/api/reports/r_fixture/takedown", params={"reason": "需要复核"}
            ).status_code
            == 202
        )
        assert client.get(f"/api/tasks/{task}/comment-questions").status_code == 451
        assert (
            client.post(f"/api/tasks/{task}/comment-questions/Q123/analyze", json={}).status_code
            == 451
        )
        assert not Runner.calls
    monkeypatch.setenv("YUQING_CONTAINER", "true")
    with TestClient(
        create_app(runtime_dir=runtime_dir, orchestrator_factory=Runner, demo_mode=False)
    ) as client:
        assert (
            client.post(f"/api/tasks/{task}/comment-questions/Q123/analyze", json={}).status_code
            == 403
        )
        assert not Runner.calls
