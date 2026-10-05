import asyncio
import json
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.render.html import render_html
from yuqing.render.ir_migrations import migrate_report
from yuqing.services.public_interest import PublicInterestDecision
from yuqing.services.task_diagnostics import refresh_report_runtime, task_timing
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate

FIXTURE = Path(__file__).parents[3] / "docs" / "方案包" / "fixtures" / "report-ir-v0.1.fixture.json"


class CoreClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromisoformat("2026-01-01T12:20:00+08:00").astimezone(tz)


class CoreThenComments:
    def __init__(self, database, events):
        self.database = database
        self.events = events

    async def status_at(self, task_id, status, phase, stamp):
        event = await self.events.emit_task_status(task_id, status=status, phase=phase, progress=90)
        await self.database.execute_write(
            "UPDATE event_log SET ts=? WHERE task_id=? AND seq=?", (stamp, task_id, event.seq)
        )

    async def run_task(self, task_id):
        for status, phase, stamp in (
            ("running", "forum", "2026-01-01T12:00:00+08:00"),
            ("paused", "forum", "2026-01-01T12:10:00+08:00"),
            ("running", "reporting", "2026-01-01T12:12:00+08:00"),
            ("running", "comment_analysis", "2026-01-01T12:20:00+08:00"),
        ):
            await self.status_at(task_id, status, phase, stamp)
        await self.database.record_llm_call(
            task_id,
            {
                "call_id": "core",
                "attempt": 1,
                "status": "complete",
                "stage": "report_draft",
                "total_tokens": 10,
                "queue_ms": 0,
                "request_ms": 100,
            },
        )
        report = migrate_report(json.loads(FIXTURE.read_text(encoding="utf-8")))
        report["report_id"] = "runtime-fixture"
        report["task"]["task_id"] = task_id
        report["quality"]["release_label"] = "evidence_brief"
        report["quality"]["comment_increment"] = {"core_preserved": True}
        with patch("yuqing.services.task_diagnostics.datetime", CoreClock):
            report["quality"]["timing"] = await task_timing(self.database, task_id)
        diagnostics = await self.database.llm_diagnostics(task_id)
        report["quality"]["call_diagnostics"] = {
            key: value for key, value in diagnostics.items() if key != "calls"
        }
        await self.database.save_report(task_id, report["report_id"], report, "fixture.html", {})
        # The reviewed comments are appended while the core statistics stay stale.
        await self.database.record_llm_call(
            task_id,
            {
                "call_id": "comments",
                "attempt": 1,
                "status": "complete",
                "stage": "scope_review",
                "total_tokens": 20,
                "queue_ms": 0,
                "request_ms": 200,
            },
        )
        await self.status_at(task_id, "done", "finished", "2026-01-01T12:25:00+08:00")


class CapturingPdf:
    html = None

    async def export(self, html, target):
        self.html = html
        await asyncio.to_thread(target.write_bytes, b"%PDF-1.7\nfixture")
        return target


async def allow(_db, _query, _note):
    return PublicInterestDecision(allowed=True, reason="fixture", category="public_event")


@pytest.mark.parametrize("view", ["ir", "html", "pdf", "evidence"])
def test_completed_report_includes_late_comment_runtime_and_requests(runtime_dir, view):
    pdf = CapturingPdf()
    with TestClient(
        create_app(
            runtime_dir=runtime_dir,
            orchestrator_factory=CoreThenComments,
            policy_checker=allow,
            pdf_exporter=pdf,
        )
    ) as client:
        task_id = client.post("/api/tasks", json={"event_query": "公开通报"}).json()["task_id"]
        client.get(f"/api/tasks/{task_id}/events")
        progress = client.get(f"/api/tasks/{task_id}/progress").json()
        assert progress["timing"]["active_seconds"] == 1380
        assert progress["timing"]["waiting_seconds"] == 120
        assert progress["model_calls"]["recorded_requests"] == 2
        if view == "ir":
            report = client.get(f"/api/tasks/{task_id}/report").json()
            assert report["quality"]["timing"]["active_seconds"] == 1380
            assert report["quality"]["timing"]["waiting_seconds"] == 120
            assert report["quality"]["timing"]["status"] == "done"
            assert report["quality"]["call_diagnostics"]["recorded_requests"] == 2
            assert "calls" not in report["quality"]["call_diagnostics"]
            assert report["quality"]["release_label"] == "evidence_brief"
            assert report["quality"]["comment_increment"]["core_preserved"] is True
            return
        endpoint = {"html": "html", "pdf": "pdf", "evidence": "evidence-package"}[view]
        response = client.get(f"/api/reports/runtime-fixture/{endpoint}")
        assert response.status_code == 200
        if view == "evidence":
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                html = archive.read("report.html").decode()
        else:
            html = pdf.html if view == "pdf" else response.text
        assert "主动运行：23 分 00 秒；用户等待：2 分 00 秒" in html
        assert "已记录模型请求：2 次" in html


@pytest.mark.parametrize(
    "seconds,expected", [(59.99, "0 分 59 秒"), (60, "1 分 00 秒"), (3951.48, "65 分 51 秒")]
)
def test_report_time_uses_the_same_seconds_precision_as_progress(seconds, expected):
    html = render_html(
        {
            "blocks": [],
            "quality": {
                "timing": {"available": True, "active_seconds": seconds, "waiting_seconds": 1457.69}
            },
        }
    )
    assert f"主动运行：{expected}；用户等待：24 分 17 秒" in html


@pytest.mark.asyncio
async def test_missing_historical_records_preserve_saved_statistics_and_limits(runtime_dir):
    database = Database(runtime_dir / "legacy.db")
    await database.initialize()
    try:
        task = await database.create_task(TaskCreate(event_query="历史公开事件"))
        quality = {
            "timing": {"available": True, "active_seconds": 600, "waiting_seconds": 0},
            "call_diagnostics": {"recorded_requests": 3},
            "acceptance": {"timing_boundary": "legacy_restart_gap_not_separated"},
            "release_label": "evidence_brief",
        }
        expected_quality = json.loads(json.dumps(quality))
        report = {"task": {"task_id": task.id}, "blocks": [], "quality": quality}
        await refresh_report_runtime(database, report)
        assert report["quality"] == expected_quality
        html = render_html(report)
        assert "累计运行状态时长：10 分 00 秒" in html
        assert "不能当作精确主动耗时" in html
        unknown = {"task": {"task_id": task.id}, "blocks": []}
        await refresh_report_runtime(database, unknown)
        assert not unknown["quality"].get("timing", {}).get("available")
        assert "主动运行：" not in render_html(unknown)
    finally:
        await database.close()
