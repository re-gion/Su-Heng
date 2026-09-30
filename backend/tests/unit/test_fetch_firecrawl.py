import json

import httpx
import pytest

from yuqing.core.fetch.base import FetchError, FetchResult
from yuqing.core.fetch.chain import FetchChain
from yuqing.core.fetch.firecrawl import FirecrawlCloudProvider


@pytest.mark.asyncio
async def test_firecrawl_posts_single_page_scrape_and_parses_markdown():
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# 事件标题\n\n这是正文，[来源](https://example.com/source)。",
                    "metadata": {"sourceURL": "https://93.184.216.34/final"},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FirecrawlCloudProvider(
        api_key="fc-test",
        client=client,
        base_url="https://firecrawl.test/v1",
    )
    try:
        result = await provider.fetch("https://93.184.216.34/original")
    finally:
        await client.aclose()
    assert seen["url"] == "https://firecrawl.test/v1/scrape"
    assert seen["authorization"] == "Bearer fc-test"
    assert json.loads(str(seen["payload"]))["formats"] == ["markdown", "html"]
    assert result.url == "https://93.184.216.34/final"
    assert result.content_type == "text/markdown"
    assert "事件标题" in result.content_text
    assert "来源" in result.content_text


@pytest.mark.asyncio
async def test_firecrawl_parses_html_and_classifies_quota_errors():
    async def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "html": "<html><body><main><h1>标题</h1><p>HTML 正文</p></main></body></html>"
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(html_handler))
    provider = FirecrawlCloudProvider(
        api_key="fc-test", client=client, base_url="https://firecrawl.test/v1"
    )
    try:
        result = await provider.fetch("https://93.184.216.34/html")
    finally:
        await client.aclose()
    assert result.content_type == "text/html"
    assert result.content_text == "标题\nHTML 正文"

    async def quota_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402, json={"success": False, "error": "Monthly credits quota exceeded"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(quota_handler))
    provider = FirecrawlCloudProvider(
        api_key="fc-test", client=client, base_url="https://firecrawl.test/v1"
    )
    try:
        with pytest.raises(FetchError, match="quota exceeded") as error:
            await provider.fetch("https://93.184.216.34/quota")
    finally:
        await client.aclose()
    assert error.value.kind == "quota"
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_fetch_chain_uses_firecrawl_only_for_challenge_or_retry_exhaustion():
    calls: list[str] = []

    class Builtin:
        async def fetch(self, url: str) -> FetchResult:
            calls.append("builtin")
            raise FetchError("挑战页", kind="challenge")

    class Firecrawl:
        async def fetch(self, url: str) -> FetchResult:
            calls.append("firecrawl")
            return FetchResult(
                url=url,
                html="<p>fallback</p>",
                content_text="fallback",
                content_type="text/html",
            )

    result = await FetchChain(Builtin(), Firecrawl()).fetch(
        "https://93.184.216.34/page", allow_fallback=True
    )
    assert result.content_text == "fallback"
    assert calls == ["builtin", "firecrawl"]

    calls.clear()

    class Permanent:
        async def fetch(self, url: str) -> FetchResult:
            calls.append("permanent")
            raise FetchError("拒绝", kind="http_status", status_code=403)

    with pytest.raises(FetchError, match="拒绝"):
        await FetchChain(Permanent(), Firecrawl()).fetch("https://93.184.216.34/page")
    assert calls == ["permanent"]


@pytest.mark.asyncio
async def test_fetch_chain_rejects_client_download_shell_as_empty_body():
    class Builtin:
        async def fetch(self, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                html="<main>更多资讯请下载央视新闻客户端</main>",
                content_text="更多资讯请下载央视新闻客户端",
                content_type="text/html",
            )

    with pytest.raises(FetchError, match="未返回可用正文") as error:
        await FetchChain(Builtin()).fetch("https://example.org/article")

    assert error.value.kind == "empty"


@pytest.mark.asyncio
async def test_fetch_chain_falls_back_when_builtin_cannot_parse_pdf():
    class Builtin:
        async def fetch(self, url: str) -> FetchResult:
            raise FetchError("PDF 文本解析失败", kind="pdf_parse")

    class Firecrawl:
        async def fetch(self, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                html="",
                content_text="PDF 正文",
                content_type="text/markdown",
            )

    result = await FetchChain(Builtin(), Firecrawl()).fetch(
        "https://93.184.216.34/file.pdf", allow_fallback=True
    )

    assert result.content_text == "PDF 正文"


@pytest.mark.asyncio
async def test_fetch_chain_respects_selection_and_per_task_fallback_cap():
    calls = 0

    class Builtin:
        async def fetch(self, url: str) -> FetchResult:
            raise FetchError("挑战页", kind="challenge")

    class Firecrawl:
        async def fetch(self, url: str) -> FetchResult:
            nonlocal calls
            calls += 1
            return FetchResult(
                url=url,
                html="",
                content_text="回退正文",
                content_type="text/markdown",
            )

    chain = FetchChain(Builtin(), Firecrawl(), max_fallback_calls=1)

    with pytest.raises(FetchError, match="挑战页"):
        await chain.fetch("https://93.184.216.34/low-value", allow_fallback=False)
    assert calls == 0

    assert (
        await chain.fetch("https://93.184.216.34/key-evidence", allow_fallback=True)
    ).content_text == "回退正文"
    with pytest.raises(FetchError, match="Firecrawl 回退已达 1 页上限"):
        await chain.fetch("https://93.184.216.34/another-key", allow_fallback=True)
    assert calls == 1
