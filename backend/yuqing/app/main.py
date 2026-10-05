import asyncio
import hashlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from yuqing.agents.comment_analysis import OpenAICommentAgent
from yuqing.agents.loader import load_definitions
from yuqing.agents.openai_runtime import OpenAIInvestigationAgent
from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.comments import adapter_for_url
from yuqing.core.events import EventBus
from yuqing.core.fetch import BuiltinFetchProvider, FetchChain, FirecrawlCloudProvider
from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway, sanitize_upstream_message
from yuqing.core.search.chain import SearchChain
from yuqing.core.search.langsearch import LangSearchLimiter, LangSearchProvider
from yuqing.core.search.providers import (
    BochaSearchProvider,
    ExaSearchProvider,
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
from yuqing.services.budget import (
    DEFAULT_BUDGET_TABLE,
    parse_budget_overrides,
    resolve_budget_table,
)
from yuqing.services.comment_plugin import (
    CommentPluginService,
    OpenAICandidateEvaluator,
    PlaywrightCommentCollector,
    PlaywrightPublicCandidateDiscoverer,
)
from yuqing.services.configuration import DEFAULT_SEARCH_PROVIDER_ORDER, ConfigService
from yuqing.services.governance import ReportRetentionService
from yuqing.services.historical_data import HistoricalDataService
from yuqing.services.hotlist import DailyHotCollector
from yuqing.services.institution_scope import InstitutionScopeReviewer
from yuqing.services.investigation_scope import is_topic_discovery_query
from yuqing.services.moderation import OpenAIModerator
from yuqing.services.openai_verifier import OpenAIEvidenceVerifier
from yuqing.services.provider_quota import (
    ProviderQuotaManager,
    ProviderRateLimiter,
    QuotaAwareFetchProvider,
    QuotaAwareSearchProvider,
)
from yuqing.services.public_interest import (
    INSTITUTION_SCOPE_CATEGORY,
    PUBLIC_EVENT_CATEGORY,
    PolicyChecker,
    PublicInterestDecision,
    assess_public_interest,
)
from yuqing.services.report_delivery import (
    ChromiumPdfExporter,
    EvidencePackageBuilder,
    PdfExporter,
)
from yuqing.services.task_diagnostics import refresh_report_runtime, task_timing
from yuqing.services.topic_discovery import OpenAITopicQueryPlanner, TopicDiscovery
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


def _normalize_topic_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep paused tasks created by the pre-clustering contract usable."""

    candidate = dict(raw)
    title = str(candidate.get("title") or candidate.get("query") or "未命名事件").strip()
    query = str(candidate.get("query") or title).strip()
    source_name = str(candidate.get("source_name") or "历史候选来源")
    url = str(candidate.get("url") or "")
    published_at = candidate.get("published_at")
    sources = candidate.get("sources")
    if not isinstance(sources, list):
        sources = (
            [
                {
                    "url": url,
                    "title": title,
                    "source_name": source_name,
                    "published_at": published_at,
                    "role": "unknown",
                    "provider": "legacy_checkpoint",
                }
            ]
            if url
            else []
        )
    candidate.update(
        {
            "title": title,
            "query": query,
            "summary": str(candidate.get("summary") or title),
            "confidence": candidate.get("confidence") or "lead",
            "confidence_label": candidate.get("confidence_label") or "线索（待核验）",
            "score": float(candidate.get("score") or 0.0),
            "reasons": candidate.get("reasons") or ["这是旧版任务保存的候选线索。"],
            "gaps": candidate.get("gaps") or ["缺少新版证据聚类信息，选择前请人工核对。"],
            "sources": sources,
            "source_count": int(candidate.get("source_count") or len(sources)),
            "date_from": candidate.get("date_from") or published_at,
            "date_to": candidate.get("date_to") or published_at,
            "coverage_limited": bool(candidate.get("coverage_limited", True)),
            "source_name": source_name,
            "url": url,
            "published_at": published_at,
            "date_status": candidate.get("date_status") or "unknown",
        }
    )
    return candidate


class CommentDeepeningRequest(BaseModel):
    mode: Literal["existing", "follow_up"] = "existing"


def _build_default_orchestrator(runtime_dir: Path) -> OrchestratorFactory:
    definitions_dir = Path(__file__).parents[1] / "agents" / "definitions"
    skills_dir = Path(__file__).parents[1] / "agents" / "skills"

    definitions, errors = load_definitions(
        definitions_dir,
        known_tools=KNOWN_AGENT_TOOLS,
        skills_directory=skills_dir,
    )
    provider_types = {
        "langsearch": LangSearchProvider,
        "zhipu": ZhipuSearchProvider,
        "qianfan": QianfanSearchProvider,
        "bocha": BochaSearchProvider,
        "exa": ExaSearchProvider,
        "tavily": TavilySearchProvider,
        "serper": SerperSearchProvider,
    }
    langsearch_limiters: dict[str, LangSearchLimiter] = {}
    provider_rate_limiters: dict[tuple[str, str], ProviderRateLimiter] = {}
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
        quotas = ProviderQuotaManager(database)
        allow_proxy_fake_ip = environ.get("YUQING_FETCH_ALLOW_PROXY_FAKE_IP", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        builtin_fetcher = BuiltinFetchProvider(allow_proxy_fake_ip=allow_proxy_fake_ip)
        fetch_order = [
            item.strip()
            for item in environ.get("FETCH_PROVIDER_ORDER", "builtin,firecrawl").split(",")
            if item.strip() in {"builtin", "firecrawl"}
        ]
        firecrawl_key = environ.get("FIRECRAWL_API_KEY", "").strip()
        if "firecrawl" in fetch_order and firecrawl_key:
            firecrawl = QuotaAwareFetchProvider(
                FirecrawlCloudProvider(
                    api_key=firecrawl_key,
                    allow_proxy_fake_ip=allow_proxy_fake_ip,
                ),
                quotas,
                critical=True,
            )
            fetcher = FetchChain(builtin_fetcher, firecrawl)
        else:
            fetcher = FetchChain(builtin_fetcher)
        budgets = resolve_budget_table(parse_budget_overrides(environ.get("BUDGET_OVERRIDES")))
        llm_factory = LLMClientFactory(environ)
        gateway = LLMGateway(llm_factory)
        order = [name for name in DEFAULT_SEARCH_PROVIDER_ORDER if name in provider_types]
        providers = []
        for name in order:
            key = environ.get(f"{name.upper()}_API_KEY", "").strip()
            if not key:
                continue
            if name == "langsearch":
                limiter = langsearch_limiters.setdefault(
                    key, LangSearchLimiter(0.22, max_calls_per_minute=290)
                )
                raw_provider = LangSearchProvider(key, limiter=limiter)
            else:
                raw_provider = provider_types[name](key)
            interval = {
                "qianfan": 1.05,
                "bocha": 1.05,
                "exa": 0.11,
            }.get(name)
            limiter = (
                provider_rate_limiters.setdefault((name, key), ProviderRateLimiter(interval))
                if interval is not None
                else None
            )
            providers.append(
                QuotaAwareSearchProvider(
                    raw_provider,
                    quotas,
                    limiter=limiter,
                    max_calls_per_task={
                        "qianfan": 20,
                        "bocha": 10,
                        "tavily": 20,
                        "serper": 10,
                    }.get(name),
                )
            )
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
            comment_agent=OpenAICommentAgent(
                gateway,
                by_name["comment_insight"].system_prompt,
            ),
            translator=OpenAITranslator(gateway),
            scope_reviewer=InstitutionScopeReviewer(gateway),
            comment_evaluator=OpenAICandidateEvaluator(gateway),
            topic_discovery=TopicDiscovery(
                search,
                fetcher,
                planner=OpenAITopicQueryPlanner(gateway),
            ),
            usage=gateway,
            models_used=model_names,
            closeables=[llm_factory, fetcher, *(provider.client for provider in providers)],
            max_outer_rounds=3,
            max_inner_rounds=max(by_name[name].max_inner_rounds for name in agents),
            budgets=budgets,
        )

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
        app.state.comment_analysis_jobs = set()
        app.state.pdf_exporter = pdf_exporter or ChromiumPdfExporter()
        app.state.delivery = EvidencePackageBuilder(database, SnapshotStore(data_dir / "snapshots"))
        resolved = await ConfigService(database).resolved_environ()
        app.state.budgets = resolve_budget_table(
            parse_budget_overrides(resolved.get("BUDGET_OVERRIDES"))
        )
        effective_comment_enabled = (
            not is_demo
            and not in_container
            and resolved.get("YUQING_COMMENT_PLUGIN_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"}
        )
        comment_collector = PlaywrightCommentCollector(data_dir / "browser-profiles")
        app.state.comment_plugin = CommentPluginService(
            database,
            data_dir,
            enabled=effective_comment_enabled,
            collector=comment_collector,
            discoverer=PlaywrightPublicCandidateDiscoverer(),
            budgets=app.state.budgets,
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
                "local_quota": public_config["search"]["quota"].get(name),
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
            if "budget" in payload:
                # 评论插件是常驻实例，预算改了要立刻同步；orchestrator 每次跑任务
                # 重新装配，会自己读到新值。
                budgets = resolve_budget_table(
                    parse_budget_overrides(
                        (await ConfigService(database).resolved_environ()).get("BUDGET_OVERRIDES")
                    )
                )
                request.app.state.budgets = budgets
                request.app.state.comment_plugin.budgets = budgets
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
                    "bocha": BochaSearchProvider,
                    "exa": ExaSearchProvider,
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
            if decision.category == INSTITUTION_SCOPE_CATEGORY:
                if payload.investigation_scope == "general":
                    return error_response(
                        "SCOPE_CONFIRMATION_REQUIRED",
                        "公共调查范围尚不明确。请确认只调查公开事件并保护个人隐私后继续。",
                        409,
                        recoverable=True,
                        details={
                            "category": decision.category,
                            "proposed_scope": "public_event",
                            "scope_label": "调查公开事件背景、公开结论、传播和争议；普通个人匿名化，不挖掘私人信息。",
                        },
                    )
            else:
                return error_response(
                    "POLICY_BLOCKED",
                    "该请求涉及普通个人的身份、私人纠纷或个人指控，不能开展此类调查。",
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
        if decision.category == PUBLIC_EVENT_CATEGORY and payload.investigation_scope == "general":
            payload = payload.model_copy(update={"investigation_scope": "public_event"})
        payload = payload.model_copy(
            update={
                "request_kind": "topic_discovery"
                if is_topic_discovery_query(payload.event_query)
                else "event"
            }
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
            "investigation_scope": task.investigation_scope,
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
        database, _ = services(request)
        task = await existing_task(task_id, request)
        plugin: CommentPluginService = request.app.state.comment_plugin
        effective_budgets = getattr(request.app.state, "budgets", DEFAULT_BUDGET_TABLE)
        candidates = await plugin.list_candidates(task.id)
        checkpoint = await database.checkpoint(task.id, "comments:selection") or {}
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
                "posts": effective_budgets[task.depth].comment_posts,
                "comments_per_post": effective_budgets[task.depth].comments_per_post,
            },
            "query": checkpoint.get("query"),
            "discovery_attempts": checkpoint.get("discovery_attempts", []),
            "manual_entry_allowed": True,
        }

    @app.get("/api/tasks/{task_id}/topic-candidates")
    async def topic_candidates(task_id: str, request: Request):
        database, _ = services(request)
        task = await existing_task(task_id, request)
        checkpoint = await database.checkpoint(task_id, "topic:selection")
        if checkpoint is None:
            return error_response(
                "TOPIC_SELECTION_NOT_READY", "任务当前没有待选择的具体事件。", 409
            )
        return {
            "task_id": task.id,
            "phase": task.phase,
            "original_query": task.event_query,
            "items": [
                _normalize_topic_candidate(item)
                for item in checkpoint.get("candidates", [])
                if isinstance(item, dict)
            ],
            "attempts": checkpoint.get("attempts", []),
            "provider_coverage": checkpoint.get("provider_coverage"),
            "effective_time_range": checkpoint.get("effective_time_range"),
            "used_default_time_range": checkpoint.get("used_default_time_range", False),
            "manual_preflight": checkpoint.get("manual_preflight"),
            "manual_entry_allowed": True,
        }

    @app.post("/api/tasks/{task_id}/topic-selection", status_code=202)
    async def select_topic(
        task_id: str, request: Request, background: BackgroundTasks, payload: dict[str, Any]
    ):
        database, _ = services(request)
        task = await existing_task(task_id, request)
        if task.status != "paused" or task.phase != "topic_selection":
            return error_response(
                "TOPIC_SELECTION_NOT_READY", "任务当前不在具体事件选择阶段。", 409
            )
        checkpoint = await database.checkpoint(task_id, "topic:selection") or {}
        candidate_id = str(payload.get("candidate_id") or "")
        manual_query = str(payload.get("event_query") or "").strip()
        force = payload.get("force") is True
        selected_candidate = next(
            (
                item
                for item in checkpoint.get("candidates", [])
                if isinstance(item, dict) and str(item.get("id")) == candidate_id
            ),
            None,
        )
        selected = (
            str(selected_candidate.get("query") or selected_candidate.get("title") or "").strip()
            if selected_candidate
            else ""
        )
        resolved_query = selected or manual_query
        if not resolved_query or len(resolved_query) > 200:
            return error_response(
                "TOPIC_SELECTION_INVALID", "请选择候选事件，或填写 1—200 字的具体事件。", 422
            )
        if not selected and is_topic_discovery_query(resolved_query):
            return error_response(
                "TOPIC_SELECTION_TOO_BROAD", "填写的仍是宽泛主题，请补充具体事件或争议点。", 422
            )
        manual_preflight = checkpoint.get("manual_preflight") or {}
        force_allowed = (
            not selected
            and force
            and manual_preflight.get("status") == "unverified"
            and str(manual_preflight.get("query") or "").strip() == manual_query
        )
        if force and not force_allowed:
            return error_response(
                "TOPIC_FORCE_NOT_ALLOWED",
                "只有同一条手工事件通过来源预检仍无结果后，才能明确以线索继续。",
                409,
            )
        needs_preflight = bool(manual_query and not selected and not force)
        next_phase = "topic_preflight" if needs_preflight else "resuming"
        claimed = await database.claim_task_status(task_id, ("paused",), "running", next_phase)
        if not claimed:
            return error_response("TOPIC_SELECTION_CONFLICT", "任务已被其他请求处理。", 409)
        await database.set_resolved_event_query(task_id, resolved_query)
        effective_range = checkpoint.get("effective_time_range") or {}
        if not task.time_range_from and not task.time_range_to and effective_range:
            await database.set_task_time_range(
                task_id,
                str(effective_range.get("date_from") or "") or None,
                str(effective_range.get("date_to") or "") or None,
            )
        if needs_preflight:
            await database.save_checkpoint(
                task_id,
                "topic:preflight",
                {
                    "phase": "topic_preflight",
                    "original_query": task.event_query,
                    "resolved_event_query": resolved_query,
                },
            )
        else:
            await database.save_checkpoint(
                task_id,
                "topic:selected",
                {
                    "phase": "outer",
                    "next_outer_round": 1,
                    "original_query": task.event_query,
                    "resolved_event_query": resolved_query,
                    "forced_unverified": bool(force_allowed),
                    "selected_candidate": selected_candidate,
                },
            )
        background.add_task(run_safely, request, task_id, resume=True)
        return {
            "task_id": task_id,
            "status": "running",
            "phase": next_phase,
            "resolved_event_query": resolved_query,
            "preflight_required": needs_preflight,
        }

    @app.post("/api/tasks/{task_id}/topic-discovery", status_code=202)
    async def retry_topic_discovery(
        task_id: str, request: Request, background: BackgroundTasks, payload: dict[str, Any]
    ):
        database, _ = services(request)
        task = await existing_task(task_id, request)
        if task.status != "paused" or task.phase != "topic_selection":
            return error_response(
                "TOPIC_SELECTION_NOT_READY", "任务当前不在具体事件选择阶段。", 409
            )
        checkpoint = await database.checkpoint(task_id, "topic:selection") or {}
        if payload.get("window") != "three_years" or not checkpoint.get("used_default_time_range"):
            return error_response(
                "TOPIC_DISCOVERY_RETRY_INVALID",
                "当前任务不能扩展到三年窗口；显式时间范围不会被系统改写。",
                422,
            )
        current_range = checkpoint.get("effective_time_range") or {}
        date_to = str(current_range.get("date_to") or datetime.now().date().isoformat())
        date_from = (datetime.fromisoformat(date_to).date() - timedelta(days=1095)).isoformat()
        claimed = await database.claim_task_status(
            task_id, ("paused",), "running", "topic_discovery"
        )
        if not claimed:
            return error_response("TOPIC_SELECTION_CONFLICT", "任务已被其他请求处理。", 409)
        await database.save_checkpoint(
            task_id,
            "topic:retry",
            {"phase": "topic_discovery", "date_from": date_from, "date_to": date_to},
        )
        background.add_task(run_safely, request, task_id, resume=True)
        return {
            "task_id": task_id,
            "status": "running",
            "phase": "topic_discovery",
            "effective_time_range": {"date_from": date_from, "date_to": date_to},
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
                "skipped": not candidate_ids,
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
                    "resolved_event_query": task.resolved_event_query,
                    "status": task.status,
                    "phase": task.phase,
                    "depth": task.depth,
                    "outer_round": task.outer_round,
                    "resumable": (
                        task.status in {"paused", "failed"}
                        and checkpoint is not None
                        and task.phase not in {"comment_selection", "topic_selection"}
                    ),
                    "comment_selection_required": task.phase == "comment_selection",
                    "topic_selection_required": task.phase == "topic_selection",
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
        outcome = await database.checkpoint(task_id, "investigation:outcome")
        recovery = await database.checkpoint(task_id, "report:quality_recovery")
        report_quality = json.loads(report["ir_json"]).get("quality", {}) if report else {}
        return {
            "task_id": task_id,
            "investigation_outcome": outcome,
            "recovery": recovery,
            "chapter_status": report_quality.get("chapter_status", {}),
            "release_label": report_quality.get("release_label"),
            "timing": await task_timing(database, task_id),
            "call_diagnostics": {
                k: v for k, v in (await database.llm_diagnostics(task_id)).items() if k != "calls"
            },
            "event_query": row["event_query"],
            "investigation_scope": row["investigation_scope"],
            "resolved_event_query": row["resolved_event_query"],
            "status": row["status"],
            "phase": row["phase"],
            "progress": {
                "outer_round": row["outer_round"],
                "max_outer_rounds": getattr(request.app.state, "budgets", DEFAULT_BUDGET_TABLE)[
                    row["depth"]
                ].outer_rounds,
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
                and row["phase"] not in {"comment_selection", "topic_selection"}
            ),
            "comment_selection_required": row["phase"] == "comment_selection",
            "topic_selection_required": row["phase"] == "topic_selection",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @app.get("/api/tasks/{task_id}/progress")
    async def task_progress(
        task_id: str,
        request: Request,
        _task: Annotated[TaskRecord, Depends(existing_task)],
    ):
        database, events = services(request)
        # Capture the lower watermark first. Clients reject an older snapshot when
        # newer SSE events arrive while these read-only queries are in flight.
        sequence = await events.high_water(task_id)
        task = await database.get_task(task_id)
        timing = await task_timing(database, task_id)
        rows = await database.fetch_all(
            "SELECT verification_state, COUNT(*) AS n FROM claim WHERE task_id=? "
            "GROUP BY verification_state",
            (task_id,),
        )
        verification = {row["verification_state"]: row["n"] for row in rows}
        diagnostics = await database.llm_diagnostics(task_id)
        usage = await database.usage_checkpoint(task_id)
        last = await database.fetch_one(
            "SELECT ts FROM event_log WHERE task_id=? AND seq=?", (task_id, sequence)
        )
        return {
            "task_id": task_id,
            "seq": sequence,
            "status": task.status if task else _task.status,
            "phase": task.phase if task else _task.phase,
            "timing": timing,
            "last_event_at": last["ts"] if last else None,
            "verification": {"total": sum(verification.values()), **verification},
            "model_calls": {k: v for k, v in diagnostics.items() if k != "calls"},
            "budget": {
                "tokens_used": max(usage.get("tokens_used", 0), task.tokens_used if task else 0),
                "calls": max(usage.get("calls", 0), diagnostics["recorded_requests"]),
                "tokens_limit": getattr(request.app.state, "budgets", DEFAULT_BUDGET_TABLE)[
                    (task or _task).depth
                ].token_limit,
                "tokens_reserved": sum(
                    call.get("reservation_tokens", 0)
                    for call in diagnostics["calls"]
                    if call.get("status") in {"queued", "inflight"}
                ),
            },
        }

    @app.get("/api/tasks/{task_id}/diagnostics")
    async def task_diagnostics(task_id: str, request: Request):
        database, _ = services(request)
        if await database.get_task(task_id) is None:
            return error_response(
                "TASK_NOT_FOUND", "任务不存在。", 404, details={"task_id": task_id}
            )
        return {
            "task_id": task_id,
            "timing": await task_timing(database, task_id),
            "model_calls": await database.llm_diagnostics(task_id),
            "usage": await database.usage_checkpoint(task_id),
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
        if task.phase in {"comment_selection", "topic_selection"} or (
            checkpoint and checkpoint.get("phase") in {"comment_selection", "topic_selection"}
        ):
            topic_pending = task.phase == "topic_selection" or (
                checkpoint and checkpoint.get("phase") == "topic_selection"
            )
            return error_response(
                "TOPIC_SELECTION_REQUIRED" if topic_pending else "COMMENT_SELECTION_REQUIRED",
                "请先选择一个具体事件。"
                if topic_pending
                else "请先确认候选帖子或选择跳过评论采集。",
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
        if task_id in request.app.state.comment_analysis_jobs:
            return error_response(
                "COMMENT_DEEPENING_BUSY", "请等待本任务的评论深入分析结束后再删除。", 409
            )
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
            after = max(since_seq or 0, int(last_event_id or 0), 0)
        except ValueError:
            after = since_seq or 0
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

    @app.get("/api/tasks/{task_id}/comment-questions")
    async def read_comment_questions(task_id: str, request: Request):
        database, _ = services(request)
        await existing_task(task_id, request)
        row = await database.get_report_for_task(task_id)
        if not row:
            return error_response("REPORT_NOT_READY", "报告尚未生成。", 409, recoverable=True)
        if await database.report_under_review(row["id"]):
            return error_response("REPORT_UNDER_REVIEW", "报告因投诉已暂时下线复核。", 451)
        report = json.loads(row["ir_json"])
        block = next(
            (
                b
                for b in report["blocks"]
                if b["type"] == "comment_insight" and b.get("analysis_version") == 5
            ),
            None,
        )
        jobs = {}
        for question in (block or {}).get("items", []):
            state = await database.checkpoint(task_id, "comments:deep:" + question["id"])
            if state:
                status = state.get("status", "unknown")
                if (
                    status in {"queued", "running"}
                    and task_id not in request.app.state.comment_analysis_jobs
                ):
                    status = "interrupted"
                jobs[question["id"]] = {
                    "status": status,
                    "mode": state.get("mode"),
                    "message": state.get("message"),
                }
        return {
            "block": block,
            "jobs": jobs,
            "sources": {
                item["evidence_ref"]: {"title": item["title"], "url": item["url"]}
                for b in report["blocks"]
                if b["type"] == "evidence_appendix"
                for item in b["items"]
            },
            "deepening_available": not is_demo and not in_container,
        }

    async def run_comment_deepening(request: Request, task_id: str, question_id: str, mode: str):
        database, events = services(request)
        runner = None
        key = "comments:deep:" + question_id
        try:
            runner = factory(database, events)
            result = await runner.deepen_comment_question(
                task_id, question_id, follow_up=mode == "follow_up"
            )
            state = await database.checkpoint(task_id, key) or {}
            await database.save_checkpoint(task_id, key, {**state, **result, "mode": mode})
        except Exception as exc:
            state = await database.checkpoint(task_id, key) or {}
            await database.save_checkpoint(
                task_id,
                key,
                {
                    **state,
                    "status": "incomplete",
                    "mode": mode,
                    "message": sanitize_upstream_message(exc),
                },
            )
        finally:
            request.app.state.comment_analysis_jobs.discard(task_id)
            if runner is not None:
                await runner.aclose()

    @app.post("/api/tasks/{task_id}/comment-questions/{question_id}/analyze", status_code=202)
    async def analyze_comment_question(
        task_id: str,
        question_id: str,
        payload: CommentDeepeningRequest,
        request: Request,
        background: BackgroundTasks,
    ):
        database, _ = services(request)
        task = await existing_task(task_id, request)
        if is_demo or in_container:
            return error_response(
                "COMMENT_DEEPENING_DISABLED", "当前运行环境未开放评论深入分析。", 403
            )
        if task.status != "done":
            return error_response(
                "TASK_NOT_FINISHED", "请先等待当前调查完成。", 409, recoverable=True
            )
        if task_id in request.app.state.comment_analysis_jobs:
            return error_response(
                "COMMENT_DEEPENING_BUSY",
                "本任务已有评论问题正在分析，请等待完成。",
                409,
                recoverable=True,
            )
        row = await database.get_report_for_task(task_id)
        if not row:
            return error_response("REPORT_NOT_READY", "报告尚未生成。", 409, recoverable=True)
        if await database.report_under_review(row["id"]):
            return error_response("REPORT_UNDER_REVIEW", "报告因投诉已暂时下线复核。", 451)
        block = next(
            (
                b
                for b in json.loads(row["ir_json"])["blocks"]
                if b["type"] == "comment_insight" and b.get("analysis_version") == 5
            ),
            None,
        )
        if not any(
            q.get("id") == question_id and q.get("review_status") == "accepted"
            for q in (block or {}).get("items", [])
        ):
            return error_response("COMMENT_QUESTION_NOT_FOUND", "未找到对应的已审评论问题。", 404)
        if task_id in request.app.state.comment_analysis_jobs:
            return error_response(
                "COMMENT_DEEPENING_BUSY", "本任务已有评论问题正在分析。", 409, recoverable=True
            )
        # The final check and reservation have no await between them.
        request.app.state.comment_analysis_jobs.add(task_id)
        try:
            previous = await database.checkpoint(task_id, "comments:deep:" + question_id) or {}
            cached_result = {k: previous.get(k) for k in ("status", "material_fingerprint", "mode")}
            await database.save_checkpoint(
                task_id,
                "comments:deep:" + question_id,
                {"status": "queued", "mode": payload.mode, "cached_result": cached_result},
            )
        except Exception:
            request.app.state.comment_analysis_jobs.discard(task_id)
            raise
        background.add_task(run_comment_deepening, request, task_id, question_id, payload.mode)
        return {"question_id": question_id, "status": "queued", "mode": payload.mode}

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
        return await refresh_report_runtime(database, migrate_report(json.loads(report["ir_json"])))

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
        ir = await refresh_report_runtime(database, migrate_report(json.loads(report["ir_json"])))
        rendered = render_html(ir, view=view)
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
        ir = await refresh_report_runtime(database, migrate_report(json.loads(report["ir_json"])))
        html = render_html(ir, view="full")
        # 模板与品牌变化也需要更新 PDF，不能继续命中旧外观的缓存。
        render_digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
        target = data_dir / "reports" / f"{report_id}-{render_digest}.pdf"
        target_exists = await asyncio.to_thread(target.is_file)
        if not target_exists:
            try:
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
        task = await database.get_task(row["task_id"])
        if task and task.investigation_scope in {"institution", "public_event"}:
            return error_response(
                "SNAPSHOT_FORBIDDEN",
                "隐私保护调查不开放未脱敏原文快照；请使用报告中的公开来源链接。",
                403,
            )
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
