import sqlite3

from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.services.public_interest import PublicInterestDecision


class IdleOrchestrator:
    def __init__(self, database, events):
        self.database = database
        self.events = events

    async def run_task(self, task_id):
        await self.database.set_task_status(task_id, "done", "finished")

    async def resume_task(self, task_id):
        return None


def _auth_environment(monkeypatch):
    monkeypatch.setenv("YUQING_AUTH_SESSION_SECRET", "test-session-secret-with-at-least-32-bytes")
    monkeypatch.setenv("YUQING_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("YUQING_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("YUQING_SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("YUQING_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.setenv("YUQING_DOMAIN", "testserver")
    monkeypatch.setenv("YUQING_ACCOUNT_DAILY_TASK_LIMIT", "1")


def _send_code(client: TestClient, sent_codes: list[tuple[str, str]], email: str) -> None:
    response = client.post(
        "/api/auth/request-code",
        json={"email": email},
        headers={"Origin": "https://testserver"},
    )
    assert response.status_code == 200
    assert sent_codes[-1][0] == email.casefold()


def _login(client: TestClient, sent_codes: list[tuple[str, str]], email: str) -> None:
    _send_code(client, sent_codes, email)
    response = client.post(
        "/api/auth/verify-code",
        json={"email": email, "code": sent_codes[-1][1]},
        headers={"Origin": "https://testserver"},
    )
    assert response.status_code == 200
    assert client.cookies.get("yuqing_session")


def test_public_auth_scopes_tasks_and_reserves_config_for_admin(runtime_dir, monkeypatch):
    _auth_environment(monkeypatch)
    sent_codes: list[tuple[str, str]] = []

    async def allow_public_event(_database, _query, _note):
        return PublicInterestDecision(allowed=True, reason="public event", category="public_event")

    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=IdleOrchestrator,
        policy_checker=allow_public_event,
        demo_mode=False,
        auth_required=True,
        auth_mail_sender=lambda email, code: sent_codes.append((email, code)),
    )
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/tasks").status_code == 401
        assert app.docs_url is None

        _login(client, sent_codes, "admin@example.com")
        admin_cookie = client.cookies.get("yuqing_session")
        assert client.get("/api/auth/me").json()["user"]["is_admin"] is True
        assert client.get("/api/config").status_code == 200
        last_admin_delete = client.delete(
            "/api/auth/account", headers={"Origin": "https://testserver"}
        )
        assert last_admin_delete.status_code == 409
        assert last_admin_delete.json()["error"]["code"] == "LAST_ADMIN_ACCOUNT"

        client.cookies.clear()
        _login(client, sent_codes, "reader@example.com")
        reader_cookie = client.cookies.get("yuqing_session")
        assert client.get("/api/auth/me").json()["user"]["is_admin"] is False
        assert client.get("/api/config").status_code == 403

        create_response = client.post(
            "/api/tasks",
            json={"event_query": "账号隔离验证", "depth": "quick"},
            headers={"Origin": "https://testserver"},
        )
        assert create_response.status_code == 202
        task_id = create_response.json()["task_id"]
        assert client.get("/api/tasks").json()["items"][0]["task_id"] == task_id
        with sqlite3.connect(app.state.database.path) as connection:
            connection.execute(
                """INSERT INTO evidence(
                       pk,task_id,local_id,url,url_hash,title,source_domain,source_tier,
                       discovered_at,snippet
                   ) VALUES('ev_private',?,'E1','https://example.com/a','hash-private',
                            'private evidence','example.com',1,'2026-10-30T00:00:00Z','snippet')""",
                (task_id,),
            )
            connection.execute(
                """INSERT INTO report(id,task_id,ir_json,metrics,generated_at)
                   VALUES('r_private',?,'{}','{}','2026-10-30T00:00:00Z')""",
                (task_id,),
            )

        second_create = client.post(
            "/api/tasks",
            json={"event_query": "触发账号每日限额", "depth": "quick"},
            headers={"Origin": "https://testserver"},
        )
        assert second_create.status_code == 429
        assert second_create.json()["error"]["code"] == "ACCOUNT_DAILY_LIMIT"

        client.cookies.clear()
        admin_headers = {"Cookie": f"yuqing_session={admin_cookie}"}
        assert client.get(f"/api/tasks/{task_id}", headers=admin_headers).status_code == 404
        assert client.get("/api/reports/r_private/html", headers=admin_headers).status_code == 404
        assert (
            client.get("/api/evidence/ev_private/snapshot", headers=admin_headers).status_code
            == 404
        )
        assert client.get("/api/tasks", headers=admin_headers).json()["items"] == []

        reader_headers = {"Cookie": f"yuqing_session={reader_cookie}"}
        assert client.get(f"/api/tasks/{task_id}", headers=reader_headers).status_code == 200

        no_origin = client.post("/api/tasks", json={"event_query": "缺少来源校验"})
        assert no_origin.status_code == 403

        client.post(
            "/api/auth/logout",
            headers={"Origin": "https://testserver", **reader_headers},
        )
        assert client.get("/api/tasks").status_code == 401


def test_auth_code_is_one_time_and_is_not_stored_in_plaintext(runtime_dir, monkeypatch):
    _auth_environment(monkeypatch)
    sent_codes: list[tuple[str, str]] = []
    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=IdleOrchestrator,
        demo_mode=False,
        auth_required=True,
        auth_mail_sender=lambda email, code: sent_codes.append((email, code)),
    )
    with TestClient(app, base_url="https://testserver") as client:
        _send_code(client, sent_codes, "person@example.com")
        person_code = sent_codes[-1][1]
        forwarded_ip = "203.0.113.8"
        forwarded = client.post(
            "/api/auth/request-code",
            json={"email": "forwarded@example.com"},
            headers={"Origin": "https://testserver", "X-Real-IP": forwarded_ip},
        )
        assert forwarded.status_code == 200
        proxy_ip_key = app.state.auth_service._key_hash("ip", forwarded_ip)
        with sqlite3.connect(app.state.database.path) as connection:
            stored_hash = connection.execute(
                "SELECT code_hash FROM auth_challenge WHERE email=?", ("person@example.com",)
            ).fetchone()[0]
            assert connection.execute(
                "SELECT 1 FROM auth_rate_limit WHERE key_hash=?", (proxy_ip_key,)
            ).fetchone()
        assert stored_hash != person_code

        verified = client.post(
            "/api/auth/verify-code",
            json={"email": "person@example.com", "code": person_code},
            headers={"Origin": "https://testserver"},
        )
        assert verified.status_code == 200
        repeated = client.post(
            "/api/auth/verify-code",
            json={"email": "person@example.com", "code": person_code},
            headers={"Origin": "https://testserver"},
        )
        assert repeated.status_code == 401


def test_auth_configuration_fails_closed_without_required_settings(monkeypatch):
    monkeypatch.delenv("YUQING_AUTH_SESSION_SECRET", raising=False)
    monkeypatch.delenv("YUQING_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("YUQING_SMTP_HOST", raising=False)
    monkeypatch.delenv("YUQING_SMTP_FROM", raising=False)
    monkeypatch.delenv("YUQING_PUBLIC_ORIGIN", raising=False)
    try:
        create_app(auth_required=True, demo_mode=False)
    except ValueError as exc:
        assert "YUQING_AUTH_SESSION_SECRET" in str(exc)
    else:
        raise AssertionError("auth-enabled app must reject missing session secret")


def test_auth_configuration_rejects_domain_origin_mismatch(monkeypatch):
    _auth_environment(monkeypatch)
    monkeypatch.setenv("YUQING_DOMAIN", "other.example.com")
    try:
        create_app(auth_required=True, demo_mode=False)
    except ValueError as exc:
        assert "YUQING_DOMAIN" in str(exc)
    else:
        raise AssertionError("auth-enabled app must reject a mismatched public domain")


def test_account_deletion_removes_owned_tasks_and_stops_active_deletion(runtime_dir, monkeypatch):
    _auth_environment(monkeypatch)
    sent_codes: list[tuple[str, str]] = []

    async def allow_public_event(_database, _query, _note):
        return PublicInterestDecision(allowed=True, reason="public event", category="public_event")

    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=IdleOrchestrator,
        policy_checker=allow_public_event,
        demo_mode=False,
        auth_required=True,
        auth_mail_sender=lambda email, code: sent_codes.append((email, code)),
    )
    with TestClient(app, base_url="https://testserver") as client:
        _login(client, sent_codes, "delete-me@example.com")
        created = client.post(
            "/api/tasks",
            json={"event_query": "账号删除验证", "depth": "quick"},
            headers={"Origin": "https://testserver"},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]

        with sqlite3.connect(app.state.database.path) as connection:
            connection.execute("UPDATE task SET status='running' WHERE id=?", (task_id,))
        blocked = client.delete("/api/auth/account", headers={"Origin": "https://testserver"})
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "ACCOUNT_TASKS_BUSY"

        with sqlite3.connect(app.state.database.path) as connection:
            connection.execute("UPDATE task SET status='done' WHERE id=?", (task_id,))
        deleted = client.delete("/api/auth/account", headers={"Origin": "https://testserver"})
        assert deleted.status_code == 200
        assert deleted.json()["deleted_tasks"] == 1
        assert client.get("/api/auth/me").status_code == 401
        email_key = app.state.auth_service.email_rate_limit_key("delete-me@example.com")
        with sqlite3.connect(app.state.database.path) as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM user_account WHERE email=?", ("delete-me@example.com",)
                ).fetchone()
                is None
            )
            assert (
                connection.execute("SELECT 1 FROM task WHERE id=?", (task_id,)).fetchone() is None
            )
            assert (
                connection.execute(
                    "SELECT 1 FROM auth_rate_limit WHERE key_hash=?", (email_key,)
                ).fetchone()
                is None
            )


def test_auth_disabled_mode_preserves_local_use(runtime_dir):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app) as client:
        assert client.get("/api/auth/me").json() == {"auth_required": False, "user": None}
