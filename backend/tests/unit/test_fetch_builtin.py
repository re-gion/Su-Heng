import httpx
import pytest

from yuqing.core.fetch.builtin import BuiltinFetchProvider, FetchError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "<html><title>安全验证</title><body>请完成验证码后继续访问</body></html>",
        "<html><title>Sign in</title><body>Please sign in to continue</body></html>",
        "<html><title>Captcha</title><body>Verify you are human</body></html>",
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
