from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from typing import Any

import httpx

from yuqing.core.fetch.base import FetchError, FetchResult
from yuqing.core.fetch.builtin import _extract_text, _looks_like_access_challenge, _normalise_text
from yuqing.core.fetch.security import UnsafeUrlError, validate_public_url

__all__ = [
    "FirecrawlCloudProvider",
    "FirecrawlFetchProvider",
    "FirecrawlProvider",
    "FirecrawlError",
]


_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_QUOTA_MARKERS = (
    "credit",
    "quota",
    "monthly limit",
    "billing",
    "insufficient balance",
    "额度",
    "配额",
    "余额",
)


class FirecrawlError(FetchError):
    """A Firecrawl-specific, still provider-independent classified error."""


def _markdown_to_text(markdown: str) -> str:
    # Firecrawl Markdown is already the provider's cleaned main content. Keep
    # headings and list text, while removing URL noise that is not useful as
    # evidence prose.
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<https?://[^>]+>", "", value)
    value = re.sub(r"[*_~]", "", value)
    value = re.sub(r"`{1,3}", "", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.MULTILINE)
    return _normalise_text(value)


def _first_text(mapping: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return ""


class FirecrawlCloudProvider:
    """Single-page Firecrawl Cloud scraper.

    This adapter intentionally exposes only ``scrape`` semantics. It never
    invokes crawl/search/browser endpoints and is intended as a low-volume
    fallback for selected evidence pages.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.firecrawl.dev/v1",
        endpoint: str | None = None,
        allow_proxy_fake_ip: bool = False,
        max_retries: int = 1,
        backoff_base: float = 0.5,
        max_backoff: float = 5.0,
        timeout: httpx.Timeout | None = None,
        max_response_bytes: int = 15 * 1024 * 1024,
    ) -> None:
        self.api_key = (api_key or os.environ.get("FIRECRAWL_API_KEY", "")).strip()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(60, connect=15),
            headers={
                "User-Agent": "YuQingAgent/0.3 (+public evidence research)",
                "Accept": "application/json",
            },
        )
        clean_base = base_url.rstrip("/")
        self.endpoint = endpoint or (
            clean_base if clean_base.endswith("/scrape") else f"{clean_base}/scrape"
        )
        self.allow_proxy_fake_ip = allow_proxy_fake_ip
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.0, backoff_base)
        self.max_backoff = max(0.0, max_backoff)
        self.max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _wait_before_retry(self, number: int, headers: Mapping[str, str]) -> None:
        value = headers.get("retry-after")
        try:
            delay = min(max(float(value), 0.0), self.max_backoff) if value else None
        except ValueError:
            delay = None
        if delay is None:
            delay = min(self.backoff_base * (2**number), self.max_backoff)
        if delay:
            await asyncio.sleep(delay)

    @staticmethod
    def _error_text(payload: Any) -> str:
        if isinstance(payload, Mapping):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, Mapping):
                    nested = FirecrawlCloudProvider._error_text(value)
                    if nested:
                        return nested
        return "Firecrawl 返回了未说明原因的错误"

    async def fetch(self, url: str) -> FetchResult:
        try:
            requested_url = validate_public_url(url, allow_proxy_fake_ip=self.allow_proxy_fake_ip)
        except UnsafeUrlError:
            # Preserve the security exception type/message; a chain must not
            # turn an unsafe target into a Cloud fallback opportunity.
            raise
        if not self.api_key:
            raise FirecrawlError(
                "Firecrawl Cloud 未配置 API Key",
                kind="provider_unavailable",
            )

        retries = 0
        while True:
            try:
                response = await self.client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "url": requested_url,
                        "formats": ["markdown", "html"],
                        "onlyMainContent": True,
                    },
                )
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if retries < self.max_retries:
                        await self._wait_before_retry(retries, response.headers)
                        retries += 1
                        continue
                    raise FirecrawlError(
                        f"Firecrawl 返回可重试 HTTP 状态 {response.status_code}，已耗尽重试次数",
                        kind="retry_exhausted",
                        retryable=True,
                        retry_exhausted=True,
                        status_code=response.status_code,
                    )
                if response.status_code >= 400:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    detail = self._error_text(payload)
                    lowered = detail.lower()
                    kind = (
                        "quota"
                        if any(marker in lowered for marker in _QUOTA_MARKERS)
                        else "http_status"
                    )
                    raise FirecrawlError(
                        f"Firecrawl 请求失败（HTTP {response.status_code}）：{detail}",
                        kind=kind,
                        status_code=response.status_code,
                    )
                raw = await response.aread()
                if len(raw) > self.max_response_bytes:
                    raise FirecrawlError("Firecrawl 响应超过大小上限", kind="too_large")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise FirecrawlError(
                        "Firecrawl 返回了无法解析的 JSON", kind="invalid_response"
                    ) from exc
                return self._parse_payload(payload, requested_url)
            except FirecrawlError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if retries < self.max_retries:
                    await self._wait_before_retry(retries, {})
                    retries += 1
                    continue
                raise FirecrawlError(
                    f"Firecrawl 网络请求失败，已耗尽重试次数：{type(exc).__name__}",
                    kind="retry_exhausted",
                    retryable=True,
                    retry_exhausted=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise FirecrawlError(
                    f"Firecrawl 请求失败：{type(exc).__name__}", kind="request"
                ) from exc

    def _parse_payload(self, payload: Any, requested_url: str) -> FetchResult:
        if not isinstance(payload, Mapping):
            raise FirecrawlError("Firecrawl 返回格式不是对象", kind="invalid_response")
        if payload.get("success") is False:
            detail = self._error_text(payload)
            lowered = detail.lower()
            kind = (
                "quota" if any(marker in lowered for marker in _QUOTA_MARKERS) else "provider_error"
            )
            raise FirecrawlError(f"Firecrawl 抓取失败：{detail}", kind=kind)

        data = payload.get("data")
        if not isinstance(data, Mapping):
            data = payload
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        final_url = (
            _first_text(
                metadata,
                "sourceURL",
                "sourceUrl",
                "url",
            )
            or _first_text(data, "url", "sourceURL", "sourceUrl")
            or requested_url
        )
        try:
            final_url = validate_public_url(final_url, allow_proxy_fake_ip=self.allow_proxy_fake_ip)
        except UnsafeUrlError as exc:
            raise FirecrawlError(
                "Firecrawl 返回了不安全的最终 URL", kind="unsafe_redirect"
            ) from exc

        markdown = _first_text(data, "markdown", "md", "content")
        html = _first_text(data, "html")
        if _looks_like_access_challenge(markdown) or _looks_like_access_challenge(html):
            raise FirecrawlError(
                "Firecrawl 返回了登录或人机验证页面",
                kind="challenge",
            )
        content_text = _markdown_to_text(markdown) if markdown else ""
        if not content_text and html:
            content_text = _extract_text(html, is_html=True)
        if not content_text:
            raise FirecrawlError("Firecrawl 未返回有效正文", kind="empty")
        content_type = "text/markdown" if markdown and not html else "text/html"
        return FetchResult(
            url=final_url,
            html=html,
            content_text=content_text,
            content_type=content_type,
        )


FirecrawlFetchProvider = FirecrawlCloudProvider
FirecrawlProvider = FirecrawlCloudProvider
