from fastapi.testclient import TestClient

from yuqing.app.main import create_app


class IdleOrchestrator:
    async def run_task(self, task_id):
        pass


def test_cursor_reaches_every_task_even_with_equal_timestamps(runtime_dir):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:

        async def seed():
            for index in range(45):
                await app.state.database.execute_write(
                    """INSERT INTO task(id,event_query,depth,status,created_at,updated_at)
                       VALUES (?,?,'quick','queued',?,?)""",
                    (
                        f"ledger-{index:03}",
                        "分页测试",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

        client.portal.call(seed)
        from yuqing.core.events import EventBus

        client.portal.call(
            lambda: EventBus(app.state.database).emit_task_status(
                "ledger-044", status="done", phase="finished", progress=100
            )
        )
        first = client.get("/api/tasks", params={"limit": 8}).json()["items"][0]
        assert first["created_at"] == "2026-01-01T00:00:00+00:00"
        assert first["completed_at"] is not None
        ids = []
        cursor = None
        while True:
            response = client.get(
                "/api/tasks", params={"limit": 8, **({"cursor": cursor} if cursor else {})}
            )
            assert response.status_code == 200
            page = response.json()
            ids.extend(item["task_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(ids) == len(set(ids)) == 45
        assert ids == sorted(ids, reverse=True)
        assert client.get("/api/tasks", params={"cursor": "broken"}).status_code == 422


def test_demo_pagination_filters_session_before_limit(runtime_dir, monkeypatch):
    import hashlib

    monkeypatch.setenv("YUQING_DEMO_MODE", "true")
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=lambda *_: IdleOrchestrator())
    with TestClient(app) as client:

        async def seed():
            for index in range(25):
                task_id = f"demo-{index:03}"
                await app.state.database.execute_write(
                    "INSERT INTO task(id,event_query,depth,status,created_at,updated_at) VALUES (?,?,'quick','queued',?,?)",
                    (task_id, "会话隔离", "2026-11-14T00:00:00+00:00", "2026-11-14T00:00:00+00:00"),
                )
                owner = "mine" if index < 5 else "others"
                await app.state.database.execute_write(
                    "INSERT INTO demo_task_owner(task_id,owner_hash,created_at) VALUES (?,?,?)",
                    (
                        task_id,
                        hashlib.sha256(owner.encode()).hexdigest(),
                        "2026-11-14T00:00:00+00:00",
                    ),
                )

        client.portal.call(seed)
        client.cookies.set("yuqing_demo_session", "mine")
        first = client.get("/api/tasks", params={"limit": 3}).json()
        assert [row["task_id"] for row in first["items"]] == ["demo-004", "demo-003", "demo-002"]
        second = client.get(
            "/api/tasks", params={"limit": 3, "cursor": first["next_cursor"]}
        ).json()
        assert [row["task_id"] for row in second["items"]] == ["demo-001", "demo-000"]
        assert second["next_cursor"] is None
