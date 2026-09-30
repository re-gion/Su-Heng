import io

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from yuqing.core.fetch.builtin import BuiltinFetchProvider, FetchError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "<html><title>安全验证</title><body>请完成验证码后继续访问</body></html>",
        "<html><title>Sign in</title><body>Please sign in to continue</body></html>",
        "<html><title>Captcha</title><body>Verify you are human</body></html>",
        "<html><body>Please enable JavaScript to continue</body></html>",
    ],
)
async def test_fetcher_stops_at_login_or_human_verification_page(body):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BuiltinFetchProvider(client=client)
    try:
        with pytest.raises(FetchError, match="登录或人机验证"):
            await provider.fetch("https://93.184.216.34/report")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetcher_decodes_meta_charset_and_keeps_final_url():
    body = '<html><head><meta charset="gbk"></head><body><article>中文正文</article></body></html>'
    payload = body.encode("gbk")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=payload, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BuiltinFetchProvider(client=client, backoff_base=0)
    try:
        result = await provider.fetch("https://93.184.216.34/start")
    finally:
        await client.aclose()
    assert result.url == "https://93.184.216.34/final"
    assert "中文正文" in result.html
    assert "中文正文" in result.content_text


@pytest.mark.asyncio
async def test_fetcher_retries_transient_status_but_not_client_error():
    calls = 0

    async def transient(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503 if calls == 1 else 200,
            text="<html><body>可用正文</body></html>",
            headers={"content-type": "text/html"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transient))
    provider = BuiltinFetchProvider(client=client, max_retries=1, backoff_base=0)
    try:
        result = await provider.fetch("https://93.184.216.34/retry")
    finally:
        await client.aclose()
    assert calls == 2
    assert result.content_text == "可用正文"

    calls = 0

    async def permanent(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(permanent))
    provider = BuiltinFetchProvider(client=client, max_retries=2, backoff_base=0)
    try:
        with pytest.raises(FetchError, match="HTTP 状态 404") as error:
            await provider.fetch("https://93.184.216.34/missing")
    finally:
        await client.aclose()
    assert calls == 1
    assert error.value.kind == "http_status"


@pytest.mark.asyncio
async def test_fetcher_extracts_text_from_pdf_with_bundled_parser():
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (PDF evidence text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=output.getvalue(),
            headers={"content-type": "application/pdf"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BuiltinFetchProvider(client=client, backoff_base=0)
    try:
        result = await provider.fetch("https://93.184.216.34/report.pdf")
    finally:
        await client.aclose()

    assert result.content_type == "application/pdf"
    assert result.content_text == "PDF evidence text"
