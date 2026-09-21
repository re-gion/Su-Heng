import asyncio
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

from yuqing.services.comment_plugin import (
    CandidateDiscoveryAttempt,
    OpenAICandidateEvaluator,
    PlaywrightCommentCollector,
    PlaywrightPublicCandidateDiscoverer,
    PublicCandidateDiscovery,
)


class TopicQueryGateway:
    def __init__(self, payload):
        self.payload = payload

    async def complete_json(self, *args, **kwargs):
        return self.payload


class FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False
        self.cookies_called = False

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def cookies(self):
        self.cookies_called = True
        return ["cookie"]

    async def close(self):
        self.closed = True


class FakePage:
    def __init__(self):
        self.visited = []

    async def goto(self, url, wait_until=None, timeout=None):  # noqa: ASYNC109
        self.visited.append(url)

    async def bring_to_front(self):
        return None


class FakeChromium:
    def __init__(self):
        self.thread_id = None
        self.loop = None
        self.args = None
        self.kwargs = None
        self.context = FakeContext()

    async def launch_persistent_context(self, *args, **kwargs):
        self.thread_id = threading.get_ident()
        self.loop = asyncio.get_running_loop()
        self.args = args
        self.kwargs = kwargs
        return self.context


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeAsyncPlaywright:
    def __init__(self, playwright: FakePlaywright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


def test_public_candidate_search_url_encodes_query_and_targets_platform():
    url = PlaywrightPublicCandidateDiscoverer.search_url("bilibili", "武汉大学 图书馆事件")

    assert url.startswith("https://search.bilibili.com/all?keyword=")
    assert "%E6%AD%A6%E6%B1%89%E5%A4%A7%E5%AD%A6%20" in url


def test_public_candidate_prefers_event_title_over_thumbnail_metrics():
    query = "武汉大学图书馆事件"

    assert PlaywrightPublicCandidateDiscoverer.title_score(
        query, "一个视频了解武汉大学图书馆事件始末"
    ) > PlaywrightPublicCandidateDiscoverer.title_score(query, "129.5万 5060 11:49")


@pytest.mark.asyncio
async def test_topic_query_expansion_keeps_subject_and_drops_drifted_queries():
    evaluator = OpenAICandidateEvaluator(
        TopicQueryGateway(
            {
                "queries": [
                    "武汉大学 图书馆 争议 校方回应",
                    "武汉大学 2025 舆情 事件",
                    "其他大学 学术争议",
                    "武汉大学 图书馆 争议 校方回应",
                ]
            }
        )
    )

    queries = await evaluator.propose_topic_queries(
        "武汉大学舆情",
        date_from="2023-01-01",
        date_to="2026-01-01",
        language="zh",
    )

    assert queries == ["武汉大学 图书馆 争议 校方回应"]


@pytest.mark.asyncio
async def test_public_candidate_discovery_uses_dedicated_worker_loop(monkeypatch):
    discoverer = PlaywrightPublicCandidateDiscoverer()
    caller_thread = threading.get_ident()
    observed: dict[str, object] = {}

    async def fake_discover(query, platforms, *, limit_per_platform):
        observed["thread"] = threading.get_ident()
        observed["loop"] = asyncio.get_running_loop()
        return PublicCandidateDiscovery(
            attempts=[CandidateDiscoveryAttempt(platform="weibo", status="empty", count=0)]
        )

    monkeypatch.setattr(discoverer, "_discover_in_loop", fake_discover)

    result = await discoverer.discover("武汉大学图书馆事件", ["weibo"])

    assert observed["thread"] != caller_thread
    if hasattr(asyncio, "ProactorEventLoop"):
        assert isinstance(observed["loop"], asyncio.ProactorEventLoop)
    assert result.attempts[0].status == "empty"


@pytest.mark.asyncio
async def test_comment_login_launches_browser_in_dedicated_worker_loop(tmp_path: Path, monkeypatch):
    fake_playwright = FakePlaywright()
    fake_async_api = ModuleType("playwright.async_api")
    fake_async_api.async_playwright = lambda: FakeAsyncPlaywright(fake_playwright)
    fake_playwright_pkg = ModuleType("playwright")
    fake_playwright_pkg.async_api = fake_async_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    monkeypatch.setenv("YUQING_COMMENT_BROWSER", "browser.exe")

    collector = PlaywrightCommentCollector(tmp_path)
    caller_thread = threading.get_ident()

    await collector.open_login(
        type(
            "Adapter",
            (),
            {
                "platform": "weibo",
                "definition": type("Def", (), {"home_url": "https://weibo.com"})(),
            },
        )()
    )

    for _ in range(50):
        if fake_playwright.chromium.thread_id is not None:
            break
        await asyncio.sleep(0.02)

    assert fake_playwright.chromium.thread_id is not None
    assert fake_playwright.chromium.thread_id != caller_thread
    assert fake_playwright.chromium.loop is not None
    if hasattr(asyncio, "ProactorEventLoop"):
        assert isinstance(fake_playwright.chromium.loop, asyncio.ProactorEventLoop)
    assert fake_playwright.chromium.context.pages[0].visited == ["https://weibo.com"]

    assert await collector.has_login("weibo") is True
    await collector.aclose()
    assert fake_playwright.stopped is True
