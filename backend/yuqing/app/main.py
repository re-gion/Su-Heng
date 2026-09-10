import asyncio
import hashlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Protocol

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from yuqing.agents.loader import load_definitions
from yuqing.agents.openai_runtime import OpenAIInvestigationAgent
from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.comments import adapter_for_url
from yuqing.core.events import EventBus
from yuqing.core.fetch.builtin import BuiltinFetchProvider
from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway, sanitize_upstream_message
from yuqing.core.search.chain import SearchChain
from yuqing.core.search.langsearch import LangSearchLimiter, LangSearchProvider
from yuqing.core.search.providers import (
    QianfanSearchProvider,
    SerperSearchProvider,
    TavilySearchProvider,
    ZhipuSearchProvider,
)
from yuqing.render.html import render_html
from yuqing.render.ir_migrations import (
    CURRENT_SCHEMA_VERSION,
    UnsupportedReportVersion,
    migrate_report,
)
from yuqing.services.comment_plugin import (
    CommentPluginService,
    OpenAICandidateEvaluator,
    PlaywrightCommentCollector,
)
from yuqing.services.configuration import ConfigService
from yuqing.services.governance import ReportRetentionService
from yuqing.services.historical_data import HistoricalDataService
from yuqing.services.hotlist import DailyHotCollector
from yuqing.services.moderation import OpenAIModerator
from yuqing.services.openai_verifier import OpenAIEvidenceVerifier
from yuqing.services.public_interest import (
    PolicyChecker,
    PublicInterestDecision,
    assess_public_interest,
)
from yuqing.services.report_delivery import (
    ChromiumPdfExporter,
    EvidencePackageBuilder,
    PdfExporter,
)
from yuqing.services.translation import OpenAITranslator
from yuqing.services.v1_orchestrator import V1Orchestrator
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate, TaskRecord
from yuqing.storage.snapshots import SnapshotStore

load_dotenv(Path(__file__).parents[3] / ".env")


class Orchestrator(Protocol):
    async def run_task(self, task_id: str) -> None: ...

    async def resume_task(self, task_id: str) -> None: ...


OrchestratorFactory = Callable[[Database, EventBus], Orchestrator]
KNOWN_AGENT_TOOLS = {
    "web_search",
    "fetch_page",
    "evidence_write",
    "evidence_search",
    "claim_write",
    "forum_post",
    "forum_read",
    "hotlist_query",
    "dataset_query",
    "read_skill",
}


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
        known_tools=KNOWN_AGENT_TOOLS,
        skills_directory=skills_dir,
    )
    allow_proxy_fake_ip = os.environ.get("YUQING_FETCH_ALLOW_PROXY_FAKE_IP", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    fetcher = BuiltinFetchProvider(allow_proxy_fake_ip=allow_proxy_fake_ip)
    provider_types = {
        "langsearch": LangSearchProvider,
        "zhipu": ZhipuSearchProvider,
        "qianfan": QianfanSearchProvider,
        "tavily": TavilySearchProvider,
        "serper": SerperSearchProvider,
    }
    langsearch_limiters: dict[str, LangSearchLimiter] = {}
    search_failures: dict[str, int] = {}
    search_opened_at: dict[str, float] = {}

    def factory(database: Database, events: EventBus) -> V1Orchestrator:
        if errors:
            raise ValueError("Agent 定义加载失败：" + "; ".join(errors))
        by_name = {definition.name: definition for definition in definitions if definition.enabled}
        required = {
            "fact_investigator",
            "media_propagation",
            "history_insight",
            "moderator",
            "reporter",
            "comment_insight",
        }
        missing = required - set(by_name)
        if missing:
            raise ValueError("缺少 Agent 定义：" + ", ".join(sorted(missing)))
        stored = {
            str(row["key"]): str(row["value"])
            for row in database._read("SELECT key,value FROM config")
        }
        environ = dict(os.environ)
        environ.update(stored)
        llm_factory = LLMClientFactory(environ)
        gateway = LLMGateway(llm_factory)
        order = [
            item.strip()
            for item in environ.get(
                "SEARCH_PROVIDER_ORDER", "langsearch,zhipu,qianfan,tavily,serper"
            ).split(",")
            if item.strip() in provider_types
        ]
        providers = []
        for name in order:
            key = environ.get(f"{name.upper()}_API_KEY", "").strip()
            if not key:
                continue
            if name == "langsearch":
                limiter = langsearch_limiters.setdefault(key, LangSearchLimiter(1.1))
                provider = LangSearchProvider(key, limiter=limiter)
            else:
                provider = provider_types[name](key)
            providers.append(provider)
        if not providers:
            raise ValueError("未配置任何可用搜索 provider")
        search = SearchChain(providers, failures=search_failures, opened_at=search_opened_at)
        agents = {
            name: OpenAIInvestigationAgent(
                gateway, by_name[name].system_prompt, by_name[name].model_role
            )
            for name in ("fact_investigator", "media_propagation", "history_insight")
        }
        model_names = {
            role: f"{llm_factory.config(role).base_url}|{llm_factory.config(role).model}"
            for role in ("analyst_a", "analyst_b", "analyst_c", "moderator", "verifier", "reporter")
        }
        return V1Orchestrator(
            database,
            events,
            search=search,
            fetcher=fetcher,
            snapshots=SnapshotStore(runtime_dir / "snapshots"),
            agents=agents,
            moderator=OpenAIModerator(gateway, by_name["moderator"].system_prompt),
            verifier=OpenAIEvidenceVerifier(gateway, llm_factory),
            reports_dir=runtime_dir / "reports",
            reporter=OpenAIReportAgent(gateway, by_name["reporter"].system_prompt),
            comment_agent=OpenAIInvestigationAgent(
                gateway,
                by_name["comment_insight"].system_prompt,
                by_name["comment_insight"].model_role,
            ),
            translator=OpenAITranslator(gateway),
            comment_evaluator=OpenAICandidateEvaluator(gateway),
            usage=gateway,
            models_used=model_names,
            closeables=[llm_factory, *(provider.client for provider in providers)],
            max_outer_rounds=3,
            max_inner_rounds=max(by_name[name].max_inner_rounds for name in agents),
        )

    async def close_resources() -> None:
        await fetcher.client.aclose()

    factory.aclose = close_resources  # type: ignore[attr-defined]

    return factory


def create_app(
    *,
    runtime_dir: Path | None = None,
    orchestrator_factory: OrchestratorFactory | None = None,
    policy_checker: PolicyChecker | None = None,
    pdf_exporter: PdfExporter | None = None,
    demo_mode: bool | None = None,
) -> FastAPI:
    data_dir = Path(runtime_dir or os.getenv("YUQING_DATA_DIR", Path.cwd() / "data")).resolve()
    factory = orchestrator_factory or _build_default_orchestrator(data_dir)
    is_demo = (
        demo_mode
        if demo_mode is not None
        else os.getenv("YUQING_DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    )
    demo_daily_limit = max(1, int(os.getenv("YUQING_DEMO_DAILY_LIMIT", "3")))
    demo_concurrency_limit = max(1, int(os.getenv("YUQING_DEMO_CONCURRENCY_LIMIT", "1")))
    hotlist_urls = [
        item.strip() for item in os.getenv("YUQING_HOTLIST_URLS", "").split(",") if item.strip()
    ]
    hotlist_platforms = [
        item.strip()
        for item in os.getenv("YUQING_HOTLIST_PLATFORMS", "weibo,zhihu,douyin,toutiao").split(",")
        if item.strip()
    ]
    hotlist_interval = max(0, int(os.getenv("YUQING_HOTLIST_INTERVAL_SECONDS", "0")))
    demo_ttl_hours = max(1, int(os.getenv("YUQING_DEMO_REPORT_TTL_HOURS", "24")))
    in_container = (
        Path("/.dockerenv").exists() or os.getenv("YUQING_CONTAINER", "").lower() == "true"
    )
    if policy_checker is None:
        if orchestrator_factory is None:
            policy_checker = assess_public_interest
        else:

            async def allow_test_policy(
                _database: Database, _event_query: str, _user_note: str | None
            ) -> PublicInterestDecision:
                return PublicInterestDecision(
                    allowed=True, reason="测试编排器默认放行", category="test"
                )

            policy_checker = allow_test_policy

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for directory in (
            data_dir / "snapshots",
            data_dir / "reports",
            data_dir / "browser-profiles",
        ):
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        database = Database(data_dir / "yuqing.db")
        await database.initialize()
        orphaned = await database.mark_orphaned_tasks()
        app.state.database = database
        app.state.events = EventBus(database)
        app.state.orphaned = orphaned
        app.state.jobs = set()
        app.state.pdf_exporter = pdf_exporter or ChromiumPdfExporter()
        app.state.delivery = EvidencePackageBuilder(database, SnapshotStore(data_dir / "snapshots"))
        resolved = await ConfigService(database).resolved_environ()
        effective_comment_enabled = (
            not is_demo
            and not in_container
            and resolved.get("YUQING_COMMENT_PLUGIN_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"}
        )
        comment_collector = PlaywrightCommentCollector(data_dir / "browser-profiles")
        app.state.comment_plugin = CommentPluginService(
            database, data_dir, enabled=effective_comment_enabled, collector=comment_collector
        )
        app.state.demo_mode = is_demo
        app.state.demo_slots = asyncio.Semaphore(demo_concurrency_limit) if is_demo else None
        app.state.service_jobs = []
        app.state.hotlist_scheduler = {
            "configured": bool(hotlist_urls and hotlist_interval),
            "last_run": None,
            "last_result": None,
        }
        collector = None
        if is_demo:
            retention = ReportRetentionService(database, data_dir, demo_ttl_hours)
            await retention.cleanup_expired()

            async def cleanup_forever() -> None:
                while True:
                    await asyncio.sleep(3600)
                    await retention.cleanup_expired()

            app.state.service_jobs.append(asyncio.create_task(cleanup_forever()))
        if hotlist_urls and hotlist_interval:
            collector = DailyHotCollector(HistoricalDataService(database), hotlist_urls)

            async def collect_forever() -> None:
                while True:
                    app.state.hotlist_scheduler["last_run"] = (
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    )
                    try:
                        result = await collector.collect(hotlist_platforms)
                        app.state.hotlist_scheduler["last_result"] = {
                            "inserted": result.inserted,
                            "platforms": result.platforms,
                        }
                    except Exception as error:
                        app.state.hotlist_scheduler["last_result"] = {
                            "inserted": 0,
                            "platforms": {},
                            "error": type(error).__name__,
                        }
                    await asyncio.sleep(hotlist_interval)

            app.state.service_jobs.append(asyncio.create_task(collect_forever()))
        yield
        for service_job in app.state.service_jobs:
            service_job.cancel()
        if app.state.service_jobs:
            await asyncio.gather(*app.state.service_jobs, return_exceptions=True)
        if collector is not None:
            await collector.aclose()
        await comment_collector.aclose()
        jobs = list(app.state.jobs)
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        # 测试/插件允许用 Orchestrator 类直接充当 factory；类上的实例 aclose
        # 不属于 factory 生命周期，不能在这里以无绑定方法调用。
        close_resources = None if isinstance(factory, type) else getattr(factory, "aclose", None)
        if close_resources is not None:
            await close_resources()
        await database.close()

    app = FastAPI(title="舆情专报 Agent", version="0.3.0", lifespan=lifespan)
    demo_cookie_name = "yuqing_demo_session"

    def demo_owner_hash(request: Request) -> str | None:
        value = request.cookies.get(demo_cookie_name)
        return hashlib.sha256(value.encode()).hexdigest() if value else None

    @app.middleware("http")
    async def demo_report_privacy(request: Request, call_next):
        if not is_demo:
            return await call_next(request)
        path = request.url.path
        task_id = None
        if path.startswith("/api/tasks/"):
            task_id = path.split("/", 4)[3]
        elif path.startswith("/api/reports/"):
            report_id = path.split("/", 4)[3]
            row = await request.app.state.database.fetch_one(
                "SELECT task_id FROM report WHERE id=?", (report_id,)
            )
            task_id = row["task_id"] if row else None
        if task_id:
            owner_hash = demo_owner_hash(request)
            if not owner_hash or not await request.app.state.database.demo_task_owned_by(
                task_id, owner_hash
            ):
                return error_response("RESOURCE_NOT_FOUND", "资源不存在。", 404)
        return await call_next(request)

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

    @app.exception_handler(UnsupportedReportVersion)
    async def unsupported_report_version(
        _request: Request, exc: UnsupportedReportVersion
    ) -> JSONResponse:
        return error_response("REPORT_IR_UNSUPPORTED", str(exc), 409, recoverable=True)

    def services(request: Request) -> tuple[Database, EventBus]:
        return request.app.state.database, request.app.state.events

    def local_plugin_request(request: Request) -> bool:
        host = (request.url.hostname or "").lower()
        client_host = (request.client.host if request.client else "").lower()
        loopback_hosts = {"127.0.0.1", "localhost", "::1", "testserver", "testclient"}
        if host not in loopback_hosts:
            return False
        if client_host not in loopback_hosts:
            return False
        origin = request.headers.get("origin")
        if not origin:
            return True
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        return (parsed.hostname or "").lower() in loopback_hosts

    def comment_plugin_access_error(request: Request):
        if is_demo or in_container:
            return error_response(
                "COMMENT_PLUGIN_UNAVAILABLE",
                "评论登录态插件在 Demo 或容器环境中不可用。",
                403,
            )
        if not local_plugin_request(request):
            return error_response(
                "COMMENT_PLUGIN_LOCAL_ONLY", "登录态插件仅允许本机同源访问。", 403
            )
        return None

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
        orchestrator: Orchestrator | None = None
        demo_slot = request.app.state.demo_slots
        if demo_slot is not None:
            await demo_slot.acquire()
        try:
            orchestrator = factory(database, events)
            if hasattr(orchestrator, "comment_plugin"):
                orchestrator.comment_plugin = request.app.state.comment_plugin
            if resume:
                await orchestrator.resume_task(task_id)
            else:
                await orchestrator.run_task(task_id)
        except Exception as exc:
            current = await database.get_task(task_id)
            if current is None or current.status in {"paused", "done"}:
                return
            await events.emit(
                task_id,
                "error",
                {
                    "code": "INTERNAL_ERROR",
                    "message": sanitize_upstream_message(exc),
                    "recoverable": True,
                },
            )
            await events.emit_task_status(task_id, status="failed", phase="finished", progress=100)
        finally:
            if orchestrator is not None:
                close = getattr(orchestrator, "aclose", None)
                if close is not None:
                    await close()
            if demo_slot is not None:
                demo_slot.release()

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        database, _ = services(request)
        wal = await database.fetch_one("PRAGMA journal_mode")
        public_config = await ConfigService(database).public()
        role_status = {
            role: (
                "configured"
                if values["effective"].get("api_key")
                and values["effective"].get("base_url")
                and values["effective"].get("model")
                else "missing"
            )
            for role, values in public_config["llm"]["roles"].items()
        }
        provider_order = public_config["search"]["provider_order"]
        search_status = [
            {
                "name": name,
                "configured": bool(public_config["search"]["keys"].get(name)),
                "breaker": "closed",
            }
            for name in provider_order
        ]
        llm_configured = all(value == "configured" for value in role_status.values())
        search_configured = any(item["configured"] for item in search_status)
        if orchestrator_factory is not None:
            llm_configured = search_configured = True
        snapshots_writable = os.access(data_dir / "snapshots", os.W_OK)
        reports_writable = os.access(data_dir / "reports", os.W_OK)
        configured = (
            search_configured and llm_configured and snapshots_writable and reports_writable
        )
        definitions, definition_errors = load_definitions(
            Path(__file__).parents[1] / "agents" / "definitions",
            known_tools=KNOWN_AGENT_TOOLS,
            skills_directory=Path(__file__).parents[1] / "agents" / "skills",
        )
        return {
            "status": "ok" if configured else "degraded",
            "version": "0.3.0",
            "ir_schema_versions": ["0.1", "0.2", CURRENT_SCHEMA_VERSION],
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
            "llm_roles": role_status,
            "search_providers": search_status,
            "agent_definitions": {
                "loaded": len(definitions),
                "rejected": len(definition_errors),
                "warnings": definition_errors
                + [warning for definition in definitions for warning in definition.warnings],
            },
            "orphan_tasks": request.app.state.orphaned,
        }

    @app.get("/api/config")
    async def read_config(request: Request) -> dict[str, Any]:
        database, _ = services(request)
        return await ConfigService(database).public()

    @app.put("/api/config")
    async def update_config(payload: dict[str, Any], request: Request):
        if is_demo:
            return error_response("DEMO_READ_ONLY", "演示站禁止修改配置。", 403)
        if os.getenv("YUQING_DEMO_MODE", "").lower() in {"1", "true", "yes"}:
            return error_response("CONFIG_WRITE_DISABLED", "演示模式下配置写入已关闭。", 403)
        if "comments" in payload:
            access_error = comment_plugin_access_error(request)
            if access_error is not None:
                return access_error
        database, _ = services(request)
        try:
            updated = await ConfigService(database).update(payload)
            if "comments" in payload:
                requested = bool((payload.get("comments") or {}).get("enabled"))
                request.app.state.comment_plugin.enabled = (
                    requested and not is_demo and not in_container
                )
            return updated
        except ValueError as exc:
            return error_response("CONFIG_INVALID", str(exc), 422, recoverable=True)

    @app.post("/api/config/test")
    async def test_config(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        if is_demo:
            return error_response("DEMO_READ_ONLY", "演示站禁止测试或读取运行密钥。", 403)
        database, _ = services(request)
        environ = await ConfigService(database).resolved_environ()
        started = time.perf_counter()
        try:
            if payload.get("kind") == "llm":
                role = payload.get("role")
                if role not in {
                    "analyst_a",
                    "analyst_b",
                    "analyst_c",
                    "moderator",
                    "verifier",
                    "reporter",
                    "utility",
                }:
                    raise ValueError("未知 LLM 角色")
                llm_factory = LLMClientFactory(environ)
                try:
                    reply, _ = await LLMGateway(llm_factory).ping(role)
                    resolved = llm_factory.config(role)
                finally:
                    await llm_factory.aclose()
                return {
                    "ok": True,
                    "kind": "llm",
                    "role": role,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "sample": reply,
                    "resolved": {"base_url": resolved.base_url, "model": resolved.model},
                }
            if payload.get("kind") == "search":
                name = str(payload.get("provider") or "")
                provider_type = {
                    "langsearch": LangSearchProvider,
                    "zhipu": ZhipuSearchProvider,
                    "qianfan": QianfanSearchProvider,
                    "tavily": TavilySearchProvider,
                    "serper": SerperSearchProvider,
                }.get(name)
                if provider_type is None:
                    raise ValueError("未知搜索 provider")
                provider = provider_type(environ.get(f"{name.upper()}_API_KEY", ""))
                try:
                    result = await provider.search(
                        __import__(
                            "yuqing.core.search.base", fromlist=["SearchParams"]
                        ).SearchParams(query="舆情", top_k=1)
                    )
                finally:
                    await provider.client.aclose()
                return {
                    "ok": True,
                    "kind": "search",
                    "provider": name,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "sample": result[0].title if result else "连接成功，无结果",
                }
            raise ValueError("kind 必须是 llm 或 search")
        except Exception as exc:
            text = str(exc)
            auth = "401" in text or "403" in text
            code = (
                "LLM_AUTH_FAILED"
                if payload.get("kind") == "llm" and auth
                else "LLM_UNREACHABLE"
                if payload.get("kind") == "llm"
                else "SEARCH_PROVIDER_UNREACHABLE"
            )
            return {
                "ok": False,
                "kind": payload.get("kind"),
                "role": payload.get("role"),
                "provider": payload.get("provider"),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": {"code": code, "message": text, "recoverable": True},
            }

    @app.post("/api/tasks", status_code=202)
    async def create_task(
        payload: TaskCreate, request: Request, background: BackgroundTasks
    ) -> Any:
        database, _ = services(request)
        if payload.comment_mode != "off" or payload.comment_urls:
            access_error = comment_plugin_access_error(request)
            if access_error is not None:
                return access_error
        demo_session = request.cookies.get(demo_cookie_name)
        if is_demo:
            demo_session = demo_session or secrets.token_urlsafe(32)
            if payload.depth == "deep":
                return error_response("DEMO_DEPTH_LIMIT", "演示站不开放深入模式。", 422)
            running = await database.fetch_one(
                "SELECT COUNT(*) AS total FROM task WHERE status IN ('queued','running','pausing','stopping')"
            )
            if running and int(running["total"]) >= demo_concurrency_limit:
                return error_response(
                    "DEMO_CONCURRENCY_LIMIT",
                    f"演示站同时最多运行 {demo_concurrency_limit} 个任务。",
                    429,
                    recoverable=True,
                )
            client_key = hashlib.sha256(demo_session.encode()).hexdigest()[:16]
        try:
            decision = await policy_checker(database, payload.event_query, payload.user_note)
        except Exception as exc:
            return error_response(
                "POLICY_CHECK_UNAVAILABLE",
                f"公共性门禁暂时不可用：{type(exc).__name__}",
                503,
                recoverable=True,
            )
        if not decision.allowed:
            return error_response(
                "POLICY_BLOCKED",
                decision.reason,
                422,
                details={"category": decision.category},
            )
        if is_demo:
            allowed = await database.consume_quota(
                f"demo:{client_key}",
                datetime.now().astimezone().date().isoformat(),
                demo_daily_limit,
            )
            if not allowed:
                return error_response(
                    "DEMO_DAILY_LIMIT",
                    f"演示站每个浏览器会话每日最多创建 {demo_daily_limit} 个任务。",
                    429,
                    recoverable=True,
                )
        task = await database.create_task(payload)
        if is_demo:
            assert demo_session is not None
            await database.bind_demo_owner(
                task.id, hashlib.sha256(demo_session.encode()).hexdigest()
            )
        background.add_task(run_safely, request, task.id)
        body = {
            "task_id": task.id,
            "status": task.status,
            "events_url": f"/api/tasks/{task.id}/events",
            "created_at": task.created_at,
        }
        if not is_demo:
            return body
        response = JSONResponse(body, status_code=202)
        response.set_cookie(
            demo_cookie_name,
            demo_session,
            httponly=True,
            samesite="strict",
            secure=os.getenv("YUQING_DEMO_COOKIE_SECURE", "false").lower() == "true",
            max_age=demo_ttl_hours * 3600,
        )
        return response

    @app.get("/api/comment-plugin/status")
    async def comment_plugin_status(request: Request) -> dict[str, Any]:
        plugin: CommentPluginService = request.app.state.comment_plugin
        return {
            "enabled": plugin.enabled,
            "available": not is_demo and not in_container and local_plugin_request(request),
            "demo_mode": is_demo,
            "container": in_container,
            "platforms": plugin.platform_status(),
            "risk_notice": "仅采集用户确认帖子；不绕过验证码或风控，评论样本不代表整体民意。",
        }

    @app.post("/api/comment-plugin/platforms/{platform}/login", status_code=202)
    async def open_comment_login(platform: str, request: Request):
        if not local_plugin_request(request):
            return error_response(
                "COMMENT_PLUGIN_LOCAL_ONLY", "登录态插件仅允许本机同源访问。", 403
            )
        plugin: CommentPluginService = request.app.state.comment_plugin
        try:
            await plugin.open_login(platform)
        except ValueError as exc:
            return error_response("COMMENT_PLATFORM_UNKNOWN", str(exc), 404)
        except RuntimeError as exc:
            return error_response("COMMENT_PLUGIN_UNAVAILABLE", str(exc), 409, recoverable=True)
        return {"platform": platform, "status": "browser_opened"}

    @app.delete("/api/comment-plugin/platforms/{platform}/profile")
    async def clear_comment_profile(
        platform: str, request: Request, payload: dict[str, Any] | None = None
    ):
        if not local_plugin_request(request):
            return error_response(
                "COMMENT_PLUGIN_LOCAL_ONLY", "登录态插件仅允许本机同源访问。", 403
            )
        if not payload or payload.get("confirm") is not True:
            return error_response("CONFIRM_REQUIRED", "清除登录数据需要明确确认。", 422)
        try:
            removed = await request.app.state.comment_plugin.clear_profile(platform)
        except ValueError as exc:
            return error_response("COMMENT_PLATFORM_UNKNOWN", str(exc), 404)
        return {"platform": platform, "removed": removed}

    @app.get("/api/tasks/{task_id}/comment-candidates")
    async def comment_candidates(task_id: str, request: Request):
        access_error = comment_plugin_access_error(request)
        if access_error is not None:
            return access_error
        task = await existing_task(task_id, request)
        plugin: CommentPluginService = request.app.state.comment_plugin
        candidates = await plugin.list_candidates(task.id)
        statuses = {item["platform"]: item for item in plugin.platform_status()}
        return {
            "task_id": task.id,
            "phase": task.phase,
            "items": [
                {
                    **item.model_dump(mode="json"),
                    "login_profile_present": statuses[item.platform]["profile_present"],
                }
                for item in candidates
            ],
            "budgets": {
                "quick": {"posts": 2, "comments_per_post": 100},
                "standard": {"posts": 5, "comments_per_post": 200},
                "deep": {"posts": 8, "comments_per_post": 300},
            }[task.depth],
        }

    async def continue_after_comment_selection(
        request: Request, task_id: str, candidate_ids: list[str]
    ) -> None:
        database, events = services(request)
        task = await database.get_task(task_id)
        if task is None:
            return
        plugin: CommentPluginService = request.app.state.comment_plugin
        try:
            summary = await plugin.collect_selected(task, candidate_ids) if candidate_ids else None
        except Exception as exc:
            await database.set_task_status(task_id, "failed", "comment_collection")
            await events.emit(
                task_id,
                "error",
                {
                    "code": "COMMENT_COLLECTION_FAILED",
                    "message": f"评论采集未完成：{type(exc).__name__}",
                    "recoverable": True,
                },
            )
            await events.emit_task_status(
                task_id, status="failed", phase="comment_collection", progress=65
            )
            return
        if summary and summary.errors:
            await events.emit(
                task_id,
                "warning",
                {"code": "COMMENT_COLLECTION_PARTIAL", "message": "；".join(summary.errors)},
            )
        await database.save_checkpoint(
            task_id,
            "comments:ready",
            {
                "phase": "comments_ready",
                "completed": summary.completed if summary else 0,
                "failed": summary.failed if summary else 0,
            },
        )
        await database.set_task_status(task_id, "running", "comment_analysis")
        await events.emit(
            task_id,
            "task.status",
            {"status": "running", "phase": "comment_analysis", "progress": 70},
        )
        await run_safely(request, task_id, resume=True)

    @app.post("/api/tasks/{task_id}/comment-selection", status_code=202)
    async def select_comments(
        task_id: str, request: Request, background: BackgroundTasks, payload: dict[str, Any]
    ):
        access_error = comment_plugin_access_error(request)
        if access_error is not None:
            return access_error
        task = await existing_task(task_id, request)
        if task.status != "paused" or task.phase != "comment_selection":
            return error_response(
                "COMMENT_SELECTION_NOT_READY", "任务当前不在评论候选确认阶段。", 409
            )
        action = payload.get("action")
        if action not in {"approve", "skip"}:
            return error_response("VALIDATION_ERROR", "action 必须是 approve 或 skip。", 422)
        plugin: CommentPluginService = request.app.state.comment_plugin
        extra_urls = payload.get("urls") or []
        if extra_urls:
            try:
                supplemented_task = task.model_copy(
                    update={"comment_urls": [*task.comment_urls, *map(str, extra_urls)]}
                )
                await plugin.discover_candidates(
                    supplemented_task,
                    [],
                )
            except ValueError as exc:
                return error_response("COMMENT_URL_UNSUPPORTED", str(exc), 422)
        candidate_ids = list(
            dict.fromkeys(str(item) for item in payload.get("candidate_ids") or [])
        )
        if action == "approve":
            if not plugin.enabled:
                return error_response("COMMENT_PLUGIN_DISABLED", "评论插件尚未启用。", 409)
            candidates = {item.id: item for item in await plugin.list_candidates(task_id)}
            extra_canonical = {
                adapter_for_url(str(url)).canonicalize(str(url)) for url in extra_urls
            }
            candidate_ids.extend(
                item.id for item in candidates.values() if item.url in extra_canonical
            )
            candidate_ids = list(dict.fromkeys(candidate_ids))
            if not candidate_ids or any(item not in candidates for item in candidate_ids):
                return error_response(
                    "COMMENT_CANDIDATE_INVALID", "请选择当前任务的候选帖子。", 422
                )
            missing = []
            for platform in sorted({candidates[item].platform for item in candidate_ids}):
                if not await plugin.has_login(platform):
                    missing.append(platform)
            if missing:
                return error_response(
                    "COMMENT_LOGIN_REQUIRED",
                    "以下平台尚未在专用浏览器登录：" + "、".join(missing),
                    409,
                    recoverable=True,
                    details={"platforms": missing},
                )
        else:
            candidate_ids = []
        try:
            claimed = await plugin.claim_selection(
                task_id,
                candidate_ids,
                next_phase="comment_collection" if candidate_ids else "comment_analysis",
            )
        except ValueError as exc:
            return error_response("COMMENT_SELECTION_INVALID", str(exc), 422)
        if not claimed:
            return error_response("COMMENT_SELECTION_CONFLICT", "任务已被其他请求处理。", 409)
        background.add_task(continue_after_comment_selection, request, task_id, candidate_ids)
        return {"task_id": task_id, "status": "running", "selected": len(candidate_ids)}

    @app.post("/api/tasks/{task_id}/comment-collection/stop", status_code=202)
    async def stop_comment_collection(task_id: str, request: Request):
        access_error = comment_plugin_access_error(request)
        if access_error is not None:
            return access_error
        await existing_task(task_id, request)
        await request.app.state.comment_plugin.stop(task_id)
        return {"task_id": task_id, "status": "stopping"}

    @app.get("/api/tasks")
    async def list_tasks(
        request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> dict[str, Any]:
        database, _ = services(request)
        tasks = await database.list_tasks(limit)
        if is_demo:
            owner_hash = demo_owner_hash(request)
            tasks = [
                task
                for task in tasks
                if owner_hash and await database.demo_task_owned_by(task.id, owner_hash)
            ]
        items = []
        for task in tasks:
            report = await database.get_report_for_task(task.id)
            checkpoint = await database.latest_checkpoint(task.id)
            items.append(
                {
                    "task_id": task.id,
                    "event_query": task.event_query,
                    "status": task.status,
                    "phase": task.phase,
                    "depth": task.depth,
                    "outer_round": task.outer_round,
                    "resumable": (
                        task.status in {"paused", "failed"}
                        and checkpoint is not None
                        and task.phase != "comment_selection"
                    ),
                    "comment_selection_required": task.phase == "comment_selection",
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
            "progress": {
                "outer_round": row["outer_round"],
                "max_outer_rounds": {"quick": 1, "standard": 2, "deep": 3}[row["depth"]],
                "agents": [],
            },
            "metrics": {
                "evidence_total": evidence_count["n"],
                "claims_total": claim_count["n"],
                "tokens_used": row["tokens_used"],
                "cost_estimate": row["cost_estimate"],
            },
            "degradations": [],
            "report_id": report["id"] if report else None,
            "resumable": (
                row["status"] in {"paused", "failed"}
                and checkpoint is not None
                and row["phase"] != "comment_selection"
            ),
            "comment_selection_required": row["phase"] == "comment_selection",
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
        if task.phase == "comment_selection" or (
            checkpoint and checkpoint.get("phase") == "comment_selection"
        ):
            return error_response(
                "COMMENT_SELECTION_REQUIRED",
                "请先确认候选帖子或选择跳过评论采集。",
                409,
                recoverable=True,
            )
        if task.status not in {"paused", "failed"} or checkpoint is None:
            return error_response(
                "TASK_NOT_RESUMABLE",
                f"任务当前状态为 {task.status}，不能续跑。",
                409,
                details={"task_id": task_id, "status": task.status},
            )
        claimed = await database.claim_task_status(
            task_id, ("paused", "failed"), "running", "resuming"
        )
        if not claimed:
            current = await database.get_task(task_id)
            return error_response(
                "TASK_NOT_RESUMABLE",
                "任务已被其他请求认领续跑。",
                409,
                details={"task_id": task_id, "status": current.status if current else "missing"},
            )
        background.add_task(run_safely, request, task_id, resume=True)
        return {"task_id": task_id, "status": "running", "resumed_from": checkpoint}

    @app.post("/api/tasks/{task_id}/pause", status_code=202)
    async def pause_task(task_id: str, request: Request):
        database, events = services(request)
        task = await database.get_task(task_id)
        if task is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        if task.status == "pausing":
            return {"task_id": task_id, "status": "pausing", "requested_at": task.updated_at}
        if task.status != "running":
            return error_response(
                "TASK_NOT_PAUSABLE", f"任务当前状态为 {task.status}，不能暂停。", 409
            )
        await database.set_task_status(task_id, "pausing")
        await events.emit(
            task_id, "task.status", {"status": "pausing", "phase": "forum", "progress": 0}
        )
        current = await database.get_task(task_id)
        return {"task_id": task_id, "status": "pausing", "requested_at": current.updated_at}

    @app.post("/api/tasks/{task_id}/stop", status_code=202)
    async def stop_task(
        task_id: str,
        request: Request,
        background: BackgroundTasks,
        payload: dict[str, Any] | None = None,
    ):
        database, events = services(request)
        task = await database.get_task(task_id)
        if task is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        if task.status == "stopping":
            return {"task_id": task_id, "status": "stopping", "will_generate_report": True}
        if task.status not in {"running", "pausing", "paused"}:
            return error_response(
                "TASK_NOT_STOPPABLE", f"任务当前状态为 {task.status}，不能停止。", 409
            )
        previous = task.status
        claimed = await database.claim_task_status(task_id, (task.status,), "stopping", "forum")
        if not claimed:
            return error_response("TASK_NOT_STOPPABLE", "任务状态已变化，请刷新后重试。", 409)
        await events.emit(
            task_id, "task.status", {"status": "stopping", "phase": "forum", "progress": 0}
        )
        if previous == "paused":
            background.add_task(run_safely, request, task_id, resume=True)
        return {"task_id": task_id, "status": "stopping", "will_generate_report": True}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, request: Request):
        database, _ = services(request)
        task = await database.get_task(task_id)
        if task is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        if task.status in {"running", "pausing", "stopping"}:
            return error_response("TASK_NOT_DELETABLE", "运行中的任务需先停止。", 409)
        deleted = await database.delete_task(task_id)
        removed_files = 0
        allowed = [(data_dir / "snapshots").resolve(), (data_dir / "reports").resolve()]

        def remove_material(raw_path: str) -> bool:
            path = Path(raw_path).resolve()
            if (
                not any(path == root or root in path.parents for root in allowed)
                or not path.is_file()
            ):
                return False
            try:
                path.unlink()
            except OSError:
                return False
            return True

        for raw_path in deleted.pop("files"):
            removed_files += int(await asyncio.to_thread(remove_material, raw_path))
        deleted["snapshot_files"] = removed_files
        return {"task_id": task_id, "deleted": deleted}

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
        if await database.report_under_review(report["id"]):
            return error_response("REPORT_UNDER_REVIEW", "报告因投诉已暂时下线复核。", 451)
        return migrate_report(json.loads(report["ir_json"]))

    @app.get("/api/reports/{report_id}/html", response_class=HTMLResponse)
    async def read_report_html(
        report_id: str,
        request: Request,
        view: Annotated[str, Query(pattern="^(brief|full)$")] = "brief",
        download: Annotated[bool, Query()] = False,
    ):
        database, _ = services(request)
        report = await database.fetch_one(
            "SELECT html_path,ir_json FROM report WHERE id=?", (report_id,)
        )
        if report is None:
            return error_response("REPORT_NOT_FOUND", "报告不存在。", 404)
        if await database.report_under_review(report_id):
            return error_response("REPORT_UNDER_REVIEW", "报告因投诉已暂时下线复核。", 451)
        rendered = render_html(migrate_report(json.loads(report["ir_json"])), view=view)
        headers = (
            {"Content-Disposition": f'attachment; filename="yuqing-{report_id}.html"'}
            if download
            else None
        )
        return HTMLResponse(rendered, headers=headers)

    @app.get("/api/reports/{report_id}/pdf", response_class=FileResponse)
    async def read_report_pdf(report_id: str, request: Request):
        database, _ = services(request)
        report = await database.fetch_one(
            "SELECT pdf_path,ir_json FROM report WHERE id=?", (report_id,)
        )
        if report is None:
            return error_response("REPORT_NOT_FOUND", "报告不存在。", 404)
        if await database.report_under_review(report_id):
            return error_response("REPORT_UNDER_REVIEW", "报告因投诉已暂时下线复核。", 451)
        target = data_dir / "reports" / f"{report_id}.pdf"
        target_exists = await asyncio.to_thread(target.is_file)
        if not target_exists:
            try:
                html = render_html(migrate_report(json.loads(report["ir_json"])), view="full")
                await request.app.state.pdf_exporter.export(html, target)
                await database.set_report_pdf(report_id, str(target))
            except Exception as exc:
                return error_response(
                    "PDF_EXPORT_UNAVAILABLE",
                    f"PDF 导出不可用：{exc}",
                    503,
                    recoverable=True,
                )
        return FileResponse(
            target,
            media_type="application/pdf",
            filename=f"yuqing-{report_id}.pdf",
        )

    @app.get("/api/reports/{report_id}/evidence-package")
    async def read_evidence_package(report_id: str, request: Request):
        database, _ = services(request)
        if await database.report_under_review(report_id):
            return error_response("REPORT_UNDER_REVIEW", "报告因投诉已暂时下线复核。", 451)
        try:
            payload = await request.app.state.delivery.build(report_id)
        except ValueError:
            return error_response("REPORT_NOT_FOUND", "报告不存在。", 404)
        return Response(
            payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="yuqing-{report_id}-evidence.zip"'
            },
        )

    @app.post("/api/reports/{report_id}/takedown", status_code=202)
    async def request_report_takedown(
        report_id: str,
        request: Request,
        reason: Annotated[str, Query(min_length=3, max_length=500)],
    ):
        database, _ = services(request)
        report = await database.fetch_one("SELECT id FROM report WHERE id=?", (report_id,))
        if report is None:
            return error_response("REPORT_NOT_FOUND", "报告不存在。", 404)
        await database.request_takedown(report_id, reason.strip())
        return {"report_id": report_id, "status": "pending", "visibility": "hidden"}

    @app.get("/api/data/status")
    async def read_data_status(request: Request) -> dict[str, Any]:
        database, _ = services(request)
        status = await database.data_status()
        return {
            "demo_mode": is_demo,
            "assets": status.get("assets", 0),
            "historical_events": status.get("historical_events", 0),
            "hot_snapshots": status.get("hot_snapshots", 0),
            "hot_coverage": {"from": status.get("hot_from"), "to": status.get("hot_to")},
            "scheduler": request.app.state.hotlist_scheduler,
            "report_ttl_hours": demo_ttl_hours if is_demo else None,
        }

    @app.get("/api/evidence/{evidence_pk}/snapshot", response_class=HTMLResponse)
    async def read_snapshot(evidence_pk: str, request: Request):
        if is_demo:
            return error_response("SNAPSHOT_FORBIDDEN", "演示站不开放部署机快照。", 403)
        database, _ = services(request)
        row = await database.evidence_by_pk(evidence_pk)
        if row is None:
            return error_response("EVIDENCE_NOT_FOUND", "证据不存在。", 404)
        if row["fetch_status"] != "fetched":
            return error_response("SNAPSHOT_NOT_AVAILABLE", "该证据没有原文快照。", 404)
        if row["kind"] == "social_comments":
            try:
                raw_snapshot = await asyncio.to_thread(
                    Path(row["snapshot_path"]).read_text, encoding="utf-8"
                )
                payload = json.loads(raw_snapshot)
            except (OSError, json.JSONDecodeError):
                return error_response("SNAPSHOT_NOT_AVAILABLE", "评论样本快照不可读取。", 404)
            return JSONResponse(
                payload,
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "X-Snapshot-Sanitized": "true",
                },
            )
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
