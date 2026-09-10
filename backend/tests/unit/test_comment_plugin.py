import asyncio
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

from yuqing.services.comment_plugin import PlaywrightCommentCollector


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
