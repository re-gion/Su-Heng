import asyncio
import json
import os
from collections.abc import AsyncIterable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Protocol

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from yuqing.agents.loader import load_definitions
from yuqing.agents.openai_runtime import OpenAIInvestigationAgent
from yuqing.core.events import EventBus
from yuqing.core.fetch.builtin import BuiltinFetchProvider
from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway
from yuqing.core.search.langsearch import LangSearchProvider
from yuqing.services.openai_verifier import OpenAIEvidenceVerifier
from yuqing.services.orchestrator import M0Orchestrator
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate, TaskRecord
from yuqing.storage.snapshots import SnapshotStore

load_dotenv()


class Orchestrator(Protocol):
    async def run_task(self, task_id: str) -> None: ...

    async def resume_task(self, task_id: str) -> None: ...


OrchestratorFactory = Callable[[Database, EventBus], Orchestrator]


def error_response(
    code: str,
    message: str,
    status: int,
    *,
    recoverable: bool = False,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message, "recoverable": recoverable}
    if details is not None:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status)


def _build_default_orchestrator(runtime_dir: Path) -> OrchestratorFactory:
    definitions_dir = Path(__file__).parents[1] / "agents" / "definitions"
    skills_dir = Path(__file__).parents[1] / "agents" / "skills"

    definitions, errors = load_definitions(
        definitions_dir,
        known_tools={"web_search", "fetch_page", "evidence_write", "claim_write"},
        skills_directory=skills_dir,
    )
    llm_factory = LLMClientFactory()
    gateway = LLMGateway(llm_factory)
    search: LangSearchProvider | None = None
    fetcher = BuiltinFetchProvider()

    def factory(database: Database, events: EventBus) -> M0Orchestrator:
        nonlocal search
        if errors or not definitions:
            raise ValueError("事实调查 Agent 定义加载失败：" + "; ".join(errors))
        if search is None:
            search = LangSearchProvider(os.getenv("LANGSEARCH_API_KEY", ""))
        return M0Orchestrator(
            database,
            events,
            search=search,
            fetcher=fetcher,
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agent=OpenAIInvestigationAgent(gateway, definitions[0].system_prompt),
            verifier=OpenAIEvidenceVerifier(gateway, llm_factory),
            reports_dir=runtime_dir / "reports",
            max_inner_rounds=definitions[0].max_inner_rounds,
        )

    async def close_resources() -> None:
        await fetcher.client.aclose()
        if search is not None:
            await search.client.aclose()
        await llm_factory.aclose()

    factory.aclose = close_resources  # type: ignore[attr-defined]

    return factory


def create_app(
    *, runtime_dir: Path | None = None, orchestrator_factory: OrchestratorFactory | None = None
) -> FastAPI:
    data_dir = Path(runtime_dir or os.getenv("YUQING_DATA_DIR", Path.cwd() / "data")).resolve()
    factory = orchestrator_factory or _build_default_orchestrator(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for directory in (data_dir / "snapshots", data_dir / "reports"):
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        database = Database(data_dir / "yuqing.db")
        await database.initialize()
        orphaned = await database.mark_orphaned_tasks()
        app.state.database = database
        app.state.events = EventBus(database)
        app.state.orphaned = orphaned
        app.state.jobs = set()
        yield
        jobs = list(app.state.jobs)
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        close_resources = getattr(factory, "aclose", None)
        if close_resources is not None:
            await close_resources()
        await database.close()

    app = FastAPI(title="舆情专报 Agent", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {"location": list(item["loc"]), "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        return error_response(
            "VALIDATION_ERROR",
            "请求参数不符合契约。",
            422,
            recoverable=True,
            details={"issues": issues},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)
        return error_response("HTTP_ERROR", str(exc.detail), exc.status_code)

    def services(request: Request) -> tuple[Database, EventBus]:
        return request.app.state.database, request.app.state.events

    async def existing_task(task_id: str, request: Request):
        database, _ = services(request)
        task = await database.get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "TASK_NOT_FOUND",
                        "message": "任务不存在。",
                        "recoverable": False,
                        "details": {"task_id": task_id},
                    }
                },
            )
        return task

    async def run_safely(request: Request, task_id: str, *, resume: bool = False) -> None:
        database, events = services(request)
        try:
            orchestrator = factory(database, events)
            if resume:
                await orchestrator.resume_task(task_id)
            else:
                await orchestrator.run_task(task_id)
        except Exception as exc:
            await database.set_task_status(task_id, "failed", "finished")
            await events.emit(
                task_id,
                "error",
                {"code": "INTERNAL_ERROR", "message": str(exc), "recoverable": True},
            )
            await events.emit(
                task_id,
                "task.status",
                {"status": "failed", "phase": "finished", "progress": 100},
            )

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        database, _ = services(request)
        wal = await database.fetch_one("PRAGMA journal_mode")
        search_configured = (
            bool(os.getenv("LANGSEARCH_API_KEY")) if orchestrator_factory is None else True
        )
        llm_configured = (
            bool(os.getenv("DEFAULT_API_KEY") or os.getenv("LLM_ANALYST_A_API_KEY"))
            and bool(os.getenv("DEFAULT_API_KEY") or os.getenv("LLM_VERIFIER_API_KEY"))
            if orchestrator_factory is None
            else True
        )
        snapshots_writable = os.access(data_dir / "snapshots", os.W_OK)
        reports_writable = os.access(data_dir / "reports", os.W_OK)
        configured = (
            search_configured and llm_configured and snapshots_writable and reports_writable
        )
        definitions, definition_errors = load_definitions(
            Path(__file__).parents[1] / "agents" / "definitions",
            known_tools={"web_search", "fetch_page", "evidence_write", "claim_write"},
            skills_directory=Path(__file__).parents[1] / "agents" / "skills",
        )
        return {
            "status": "ok" if configured else "degraded",
            "version": "0.1.0",
            "ir_schema_versions": ["0.1"],
            "db": {
                "ok": True,
                "path": str(database.path),
                "wal": str(wal[0]).lower() == "wal" if wal else False,
                "migrations": "0001",
            },
            "storage": {
                "snapshots_writable": snapshots_writable,
                "reports_writable": reports_writable,
            },
            "llm_roles": {
                "analyst_a": "configured" if llm_configured else "missing",
                "verifier": "configured" if llm_configured else "missing",
            },
            "search_providers": [
                {"name": "langsearch", "configured": search_configured, "breaker": "closed"}
            ],
            "agent_definitions": {
                "loaded": len(definitions),
                "rejected": len(definition_errors),
                "warnings": definition_errors
                + [warning for definition in definitions for warning in definition.warnings],
            },
            "orphan_tasks": request.app.state.orphaned,
        }

    @app.post("/api/tasks", status_code=202)
    async def create_task(
        payload: TaskCreate, request: Request, background: BackgroundTasks
    ) -> dict[str, Any]:
        database, _ = services(request)
        task = await database.create_task(payload)
        background.add_task(run_safely, request, task.id)
        return {
            "task_id": task.id,
            "status": task.status,
            "events_url": f"/api/tasks/{task.id}/events",
            "created_at": task.created_at,
        }

    @app.get("/api/tasks")
    async def list_tasks(
        request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> dict[str, Any]:
        database, _ = services(request)
        tasks = await database.list_tasks(limit)
        items = []
        for task in tasks:
            report = await database.get_report_for_task(task.id)
            checkpoint = await database.latest_checkpoint(task.id)
            items.append(
                {
                    "task_id": task.id,
                    "event_query": task.event_query,
                    "status": task.status,
                    "depth": task.depth,
                    "outer_round": task.outer_round,
                    "resumable": task.status in {"paused", "failed"} and checkpoint is not None,
                    "report_id": report["id"] if report else None,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                }
            )
        return {"items": items, "next_cursor": None}

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str, request: Request):
        database, _ = services(request)
        row = await database.fetch_one("SELECT * FROM task WHERE id=?", (task_id,))
        if row is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        evidence_count = await database.fetch_one(
            "SELECT COUNT(*) AS n FROM evidence WHERE task_id=?", (task_id,)
        )
        claim_count = await database.fetch_one(
            "SELECT COUNT(*) AS n FROM claim WHERE task_id=?", (task_id,)
        )
        report = await database.get_report_for_task(task_id)
        checkpoint = await database.latest_checkpoint(task_id)
        return {
            "task_id": task_id,
            "event_query": row["event_query"],
            "status": row["status"],
            "phase": row["phase"],
            "progress": {"outer_round": row["outer_round"], "max_outer_rounds": 1, "agents": []},
            "metrics": {
                "evidence_total": evidence_count["n"],
                "claims_total": claim_count["n"],
                "tokens_used": row["tokens_used"],
                "cost_estimate": row["cost_estimate"],
            },
            "degradations": [],
            "report_id": report["id"] if report else None,
            "resumable": row["status"] in {"paused", "failed"} and checkpoint is not None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    async def resume_task(task_id: str, request: Request, background: BackgroundTasks):
        database, _ = services(request)
        task = await database.get_task(task_id)
        if task is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        checkpoint = await database.latest_checkpoint(task_id)
        if task.status not in {"paused", "failed"} or checkpoint is None:
            return error_response(
                "TASK_NOT_RESUMABLE",
                f"任务当前状态为 {task.status}，不能续跑。",
                409,
                details={"task_id": task_id, "status": task.status},
            )
        background.add_task(run_safely, request, task_id, resume=True)
        return {"task_id": task_id, "status": "running", "resumed_from": checkpoint}

    @app.get("/api/tasks/{task_id}/events", response_class=EventSourceResponse)
    async def task_events(
        task_id: str,
        request: Request,
        _task: Annotated[TaskRecord, Depends(existing_task)],
        since_seq: Annotated[int | None, Query(ge=0)] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> AsyncIterable[ServerSentEvent]:
        database, events = services(request)
        try:
            after = since_seq if since_seq is not None else int(last_event_id or 0)
        except ValueError:
            after = 0
        queue = events.subscribe(task_id)
        try:
            high_water = await events.high_water(task_id)
            for event in await events.history(task_id, after_seq=after, through_seq=high_water):
                yield ServerSentEvent(
                    data=event.model_dump(mode="json"), event=event.event, id=str(event.seq)
                )
            last_sent = high_water
            current = await database.get_task(task_id)
            if current and current.status in {"done", "failed"}:
                for event in await events.history(task_id, after_seq=last_sent):
                    yield ServerSentEvent(
                        data=event.model_dump(mode="json"), event=event.event, id=str(event.seq)
                    )
                    last_sent = event.seq
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ServerSentEvent(comment="heartbeat")
                    continue
                if event.seq <= last_sent:
                    continue
                live_batch = (
                    await events.history(task_id, after_seq=last_sent, through_seq=event.seq)
                    if event.seq > last_sent + 1
                    else [event]
                )
                for live_event in live_batch:
                    yield ServerSentEvent(
                        data=live_event.model_dump(mode="json"),
                        event=live_event.event,
                        id=str(live_event.seq),
                    )
                    last_sent = live_event.seq
                    if live_event.event == "task.status" and live_event.data.get("status") in {
                        "done",
                        "failed",
                    }:
                        return
        finally:
            events.unsubscribe(task_id, queue)

    @app.get("/api/tasks/{task_id}/report")
    async def read_report(task_id: str, request: Request):
        database, _ = services(request)
        task = await database.get_task(task_id)
        if task is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        report = await database.get_report_for_task(task_id)
        if report is None:
            return error_response("REPORT_NOT_READY", "报告尚未生成。", 409, recoverable=True)
        return json.loads(report["ir_json"])

    @app.get("/api/reports/{report_id}/html", response_class=HTMLResponse)
    async def read_report_html(report_id: str, request: Request):
        database, _ = services(request)
        report = await database.fetch_one("SELECT html_path FROM report WHERE id=?", (report_id,))
        report_exists = report is not None and await asyncio.to_thread(
            Path(report["html_path"]).is_file
        )
        if not report_exists:
            return error_response("REPORT_NOT_FOUND", "报告不存在。", 404)
        return FileResponse(report["html_path"], media_type="text/html")

    @app.get("/api/evidence/{evidence_pk}/snapshot", response_class=HTMLResponse)
    async def read_snapshot(evidence_pk: str, request: Request):
        database, _ = services(request)
        row = await database.evidence_by_pk(evidence_pk)
        if row is None:
            return error_response("EVIDENCE_NOT_FOUND", "证据不存在。", 404)
        if row["fetch_status"] != "fetched":
            return error_response("SNAPSHOT_NOT_AVAILABLE", "该证据没有原文快照。", 404)
        body = SnapshotStore(data_dir / "snapshots").read_sanitized(
            row["snapshot_path"], row["content_text"]
        )
        return HTMLResponse(
            body,
            headers={
                "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "SAMEORIGIN",
                "Referrer-Policy": "no-referrer",
                "X-Snapshot-Sanitized": "true",
            },
        )

    frontend_dist = Path(__file__).parents[3] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.frontend("/", directory=frontend_dist)
    else:

        @app.get("/", response_class=HTMLResponse)
        async def development_home() -> str:
            return (
                "<h1>舆情专报 Agent</h1><p>前端尚未构建，请在 frontend 目录执行 npm run build。</p>"
            )

    return app


app = create_app()
