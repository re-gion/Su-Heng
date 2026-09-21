import json
import sqlite3
import zipfile
from functools import partial
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.services.public_interest import PublicInterestDecision
from yuqing.storage.models import EvidenceCreate
from yuqing.storage.snapshots import SnapshotStore


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


class FixturePdfExporter:
    def __init__(self):
        self.html = ""

    async def export(self, html: str, target: Path) -> Path:
        self.html = html
        await __import__("asyncio").to_thread(
            target.write_bytes, b"%PDF-1.4\nfixture PDF with citations\n%%EOF"
        )
        return target


def test_health_loads_v15_history_tools_without_definition_warnings(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["version"] == "0.3.0"
    assert response.json()["agent_definitions"]["warnings"] == []


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

    assert "速览正文" in brief.text and "仅完整版" in brief.text
    assert 'data-in-brief="false"' in brief.text
    assert "const initialView='brief'" in brief.text
    assert "仅完整版" in full.text
    assert full.headers["content-disposition"].startswith("attachment;")


def test_background_job_closes_per_task_resources(runtime_dir: Path):
    ClosingOrchestrator.closed = False
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ClosingOrchestrator)
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"event_query": "资源释放"})

    assert response.status_code == 202
    assert ClosingOrchestrator.closed is True


def test_pdf_and_evidence_package_exports_preserve_report_and_sanitized_citations(
    runtime_dir: Path,
):
    pdf = FixturePdfExporter()
    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=IdleOrchestrator,
        pdf_exporter=pdf,
    )
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "交付导出"}).json()["task_id"]
        snapshot_store = SnapshotStore(runtime_dir / "snapshots")
        evidence = client.portal.call(
            client.app.state.database.add_evidence,
            EvidenceCreate(
                task_id=task_id,
                url="https://example.com/source",
                title="公开来源",
                snippet="公开关键句",
                source_name="示例来源",
                source_tier=3,
            ),
        )
        snapshot_path, digest = snapshot_store.save(
            task_id, evidence.pk, "<script>alert(1)</script><p>公开关键句</p>"
        )
        client.portal.call(
            partial(
                client.app.state.database.update_evidence_fetched,
                task_id,
                evidence.local_id,
                content_text="公开关键句",
                snapshot_path=snapshot_path,
                content_sha256=digest,
            )
        )
        ir = {
            "schema_version": "0.2",
            "min_reader_minor": 2,
            "report_id": "r_delivery",
            "task": {"task_id": task_id, "event_query": "交付导出"},
            "metrics": {},
            "blocks": [
                {
                    "block_id": "h",
                    "type": "report_header",
                    "section": "00",
                    "event_title": "交付导出",
                    "in_brief": True,
                },
                {
                    "block_id": "e",
                    "type": "evidence_appendix",
                    "section": "09",
                    "items": [
                        {
                            "evidence_ref": evidence.local_id,
                            "title": "公开来源",
                            "url": "https://example.com/source",
                            "fetch_status": "fetched",
                            "snapshot_pk": evidence.pk,
                            "content_sha256": digest,
                            "citations": [],
                        }
                    ],
                    "in_brief": False,
                },
            ],
        }
        client.portal.call(
            client.app.state.database.save_report,
            task_id,
            "r_delivery",
            ir,
            str(runtime_dir / "reports" / "r_delivery.html"),
            {},
        )

        pdf_response = client.get("/api/reports/r_delivery/pdf")
        package_response = client.get("/api/reports/r_delivery/evidence-package")

    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert "E001" in pdf.html and "公开来源" in pdf.html
    with zipfile.ZipFile(BytesIO(package_response.content)) as archive:
        assert set(archive.namelist()) == {"report.html", "manifest.json", "snapshots/E001.html"}
        manifest = json.loads(archive.read("manifest.json"))
        report_html = archive.read("report.html").decode()
        snapshot_html = archive.read("snapshots/E001.html").decode()
    assert manifest["evidence"][0]["content_sha256"] == digest
    assert 'href="snapshots/E001.html"' in report_html
    assert "公开关键句" in snapshot_html
    assert "<script" not in snapshot_html


def test_social_comment_snapshot_endpoint_returns_sanitized_json(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "评论快照"}).json()["task_id"]
        snapshot = runtime_dir / "snapshots" / task_id / "comments.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(
            json.dumps(
                {
                    "sampling_method": "fixture",
                    "comments": [{"id": "匿名哈希", "text": "脱敏评论"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        evidence = client.portal.call(
            client.app.state.database.add_evidence,
            EvidenceCreate(
                task_id=task_id,
                url="https://weibo.com/123456/AbCdEf",
                title="评论样本",
                content_text="脱敏评论",
                fetch_status="fetched",
                fetched_at="2026-08-13T10:00:00+08:00",
                snapshot_path=str(snapshot),
                content_sha256="a" * 64,
                kind="social_comments",
            ),
        )

        response = client.get(f"/api/evidence/{evidence.pk}/snapshot")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["comments"] == [{"id": "匿名哈希", "text": "脱敏评论"}]
    assert response.headers["x-snapshot-sanitized"] == "true"


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


def test_demo_mode_is_read_only_rate_limited_and_takedown_hides_report(runtime_dir: Path):
    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=ImmediateOrchestrator,
        demo_mode=True,
    )
    with TestClient(app) as client:
        readonly = client.put("/api/config", json={"search": {"provider_order": []}})
        ids = [
            client.post("/api/tasks", json={"event_query": f"演示事件 {index}"})
            for index in range(4)
        ]
        task_id = ids[0].json()["task_id"]
        ir = {
            "schema_version": "0.2",
            "min_reader_minor": 2,
            "report_id": "r_takedown",
            "task": {"task_id": task_id, "event_query": "演示事件"},
            "metrics": {},
            "blocks": [],
        }
        client.portal.call(
            client.app.state.database.save_report,
            task_id,
            "r_takedown",
            ir,
            str(runtime_dir / "reports" / "r_takedown.html"),
            {},
        )
        takedown = client.post(
            "/api/reports/r_takedown/takedown", params={"reason": "来源方申请复核删除"}
        )
        hidden = client.get("/api/reports/r_takedown/html")
        data_status = client.get("/api/data/status")

    assert readonly.status_code == 403
    assert readonly.json()["error"]["code"] == "DEMO_READ_ONLY"
    assert [item.status_code for item in ids] == [202, 202, 202, 429]
    assert takedown.status_code == 202
    assert hidden.status_code == 451
    assert hidden.json()["error"]["code"] == "REPORT_UNDER_REVIEW"
    assert data_status.json()["demo_mode"] is True
    assert data_status.json()["historical_events"] == 0


def test_demo_mode_limits_concurrent_tasks(runtime_dir: Path):
    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=IdleOrchestrator,
        demo_mode=True,
    )
    with TestClient(app) as client:
        first = client.post("/api/tasks", json={"event_query": "并发演示事件一"})
        second = client.post("/api/tasks", json={"event_query": "并发演示事件二"})

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "DEMO_CONCURRENCY_LIMIT"


def test_demo_resources_are_private_to_browser_session(runtime_dir: Path):
    app = create_app(
        runtime_dir=runtime_dir,
        orchestrator_factory=ImmediateOrchestrator,
        demo_mode=True,
    )
    with TestClient(app) as owner:
        created = owner.post("/api/tasks", json={"event_query": "会话私有报告"})
        task_id = created.json()["task_id"]
        assert owner.get(f"/api/tasks/{task_id}").status_code == 200
        owner.portal.call(
            owner.app.state.database.save_report,
            task_id,
            "r_private",
            {"schema_version": "0.2", "report_id": "r_private", "blocks": []},
            str(runtime_dir / "reports" / "r_private.html"),
            {},
        )
        owner.cookies.clear()
        detail = owner.get(f"/api/tasks/{task_id}")
        listing = owner.get("/api/tasks")
        takedown = owner.post(
            "/api/reports/r_private/takedown", params={"reason": "陌生会话无权下架"}
        )

    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert listing.json()["items"] == []
    assert takedown.status_code == 404


def test_unknown_report_ir_returns_explicit_compatibility_error(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"event_query": "未知 IR"}).json()["task_id"]
        client.portal.call(
            client.app.state.database.save_report,
            task_id,
            "r_unknown_ir",
            {"schema_version": "9.0", "report_id": "r_unknown_ir", "blocks": []},
            str(runtime_dir / "reports" / "r_unknown_ir.html"),
            {},
        )
        response = client.get(f"/api/tasks/{task_id}/report")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_IR_UNSUPPORTED"


def test_v2_task_contract_persists_language_scope_and_comment_options(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "event_query": "CrowdStrike global outage",
                "source_scope": "global",
                "source_languages": ["zh", "en", "de"],
                "comment_mode": "hybrid",
                "comment_urls": ["https://www.zhihu.com/question/123456789"],
            },
        )
        detail = client.get(f"/api/tasks/{created.json()['task_id']}")

    assert created.status_code == 202
    assert detail.status_code == 200
    connection = sqlite3.connect(runtime_dir / "yuqing.db")
    row = connection.execute(
        "SELECT source_scope,source_languages,comment_mode,comment_urls FROM task WHERE id=?",
        (created.json()["task_id"],),
    ).fetchone()
    connection.close()
    assert row == (
        "global",
        '["zh", "en", "de"]',
        "hybrid",
        '["https://www.zhihu.com/question/123456789"]',
    )


def test_comment_selection_pause_cannot_be_bypassed_and_skip_continues(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={"event_query": "评论确认测试"})
        task_id = created.json()["task_id"]
        connection = sqlite3.connect(runtime_dir / "yuqing.db")
        connection.execute(
            "UPDATE task SET status='paused',phase='comment_selection' WHERE id=?", (task_id,)
        )
        connection.execute(
            """INSERT INTO task_state(task_id,step_key,kind,status,replay,payload,result_ref,created_at,updated_at)
               VALUES(?, 'comments:selection','round_checkpoint','settled','safe','{}',
               '{"phase":"comment_selection"}','2026-01-01','2026-01-01')""",
            (task_id,),
        )
        connection.commit()
        connection.close()

        blocked = client.post(f"/api/tasks/{task_id}/resume")
        skipped = client.post(f"/api/tasks/{task_id}/comment-selection", json={"action": "skip"})

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "COMMENT_SELECTION_REQUIRED"
    assert skipped.status_code == 202


def test_broad_topic_requires_concrete_event_selection_before_investigation(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=ImmediateOrchestrator)
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={"event_query": "武汉大学舆情"})
        task_id = created.json()["task_id"]
        connection = sqlite3.connect(runtime_dir / "yuqing.db")
        connection.execute(
            "UPDATE task SET status='paused',phase='topic_selection' WHERE id=?", (task_id,)
        )
        payload = json.dumps(
            {
                "phase": "topic_selection",
                "candidates": [
                    {
                        "id": "tc_1",
                        "title": "武汉大学图书馆事件及校方回应",
                        "query": "武汉大学图书馆事件及校方回应",
                        "url": "https://www.whu.edu.cn/example",
                    }
                ],
            },
            ensure_ascii=False,
        )
        connection.execute(
            """INSERT INTO task_state(task_id,step_key,kind,status,replay,payload,result_ref,created_at,updated_at)
               VALUES(?, 'topic:selection','round_checkpoint','settled','safe',?,?,
               '2026-01-01','2026-01-01')""",
            (task_id, payload, payload),
        )
        connection.commit()
        connection.close()

        blocked = client.post(f"/api/tasks/{task_id}/resume")
        candidates = client.get(f"/api/tasks/{task_id}/topic-candidates")
        selected = client.post(
            f"/api/tasks/{task_id}/topic-selection", json={"candidate_id": "tc_1"}
        )

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "TOPIC_SELECTION_REQUIRED"
    assert candidates.json()["items"][0]["title"] == "武汉大学图书馆事件及校方回应"
    assert selected.status_code == 202
    assert selected.json()["resolved_event_query"] == "武汉大学图书馆事件及校方回应"


def test_comment_login_state_endpoints_allow_localhost_origin(runtime_dir: Path):
    app = create_app(runtime_dir=runtime_dir, orchestrator_factory=IdleOrchestrator)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login = client.post(
            "/api/comment-plugin/platforms/weibo/login",
            headers={"Origin": "http://localhost:5173"},
        )
        config = client.put(
            "/api/config",
            headers={"Origin": "http://localhost:5173"},
            json={"comments": {"enabled": True}},
        )

    assert login.status_code == 202
    assert config.status_code == 200
