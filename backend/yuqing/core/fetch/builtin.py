from __future__ import annotations

import asyncio
import codecs
import io
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
import trafilatura

from yuqing.core.fetch.base import FetchError, FetchResult
from yuqing.core.fetch.security import validate_public_url

__all__ = ["BuiltinFetchProvider", "FetchError"]


_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_CONTENT_TYPES = frozenset({"text/plain"})
_PDF_CONTENT_TYPES = frozenset({"application/pdf"})
_CHARSET_RE = re.compile(r"(?:^|;)\s*charset\s*=\s*[\"']?\s*([^;\"'\s]+)", re.I)
_META_CHARSET_RE = re.compile(rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([^\"'\s/>]+)", re.I)
_META_HTTP_EQUIV_RE = re.compile(
    rb"<meta\b(?=[^>]*\bhttp-equiv\s*=\s*[\"']?content-type)[^>]*"
    rb"\bcontent\s*=\s*[\"'][^\"']*?charset\s*=\s*([^\"';\s>]+)",
    re.I,
)


def _looks_like_access_challenge(value: str) -> bool:
    sample = value[:12000].lower()
    return any(
        marker in sample
        for marker in (
            "请完成验证码",
            "安全验证",
            "登录后继续",
            "sign in to continue",
            "verify you are human",
            "captcha",
            "please enable javascript",
            "enable javascript to continue",
            "请开启javascript",
            "请启用javascript",
        )
    )


def _header_charset(content_type: str) -> str | None:
    match = _CHARSET_RE.search(content_type)
    return match.group(1) if match else None


def _meta_charset(payload: bytes) -> str | None:
    # A page's declaration is expected near the beginning; keeping this small
    # prevents arbitrary binary data from being scanned as an HTML document.
    head = payload[:65536]
    match = _META_CHARSET_RE.search(head) or _META_HTTP_EQUIV_RE.search(head)
    if not match:
        return None
    try:
        return match.group(1).decode("ascii")
    except UnicodeDecodeError:
        return None


def _decode_payload(payload: bytes, *, content_type: str) -> str:
    if payload.startswith(codecs.BOM_UTF8):
        return payload.decode("utf-8-sig", errors="replace")
    if payload.startswith(codecs.BOM_UTF16_LE):
        return payload.decode("utf-16", errors="replace")
    if payload.startswith(codecs.BOM_UTF16_BE):
        return payload.decode("utf-16-be", errors="replace")

    candidates: list[str] = []
    for encoding in (_header_charset(content_type), _meta_charset(payload), "utf-8", "gb18030"):
        if encoding and encoding.lower() not in {value.lower() for value in candidates}:
            candidates.append(encoding)
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _normalise_text(value: str) -> str:
    value = unescape(value).replace("\x00", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


class _VisibleTextParser(HTMLParser):
    """Small stdlib fallback for pages trafilatura cannot parse."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "canvas"})
    _BLOCK = frozenset(
        {
            "address",
            "article",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "main",
            "p",
            "pre",
            "section",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._preferred_depth = 0
        self._preferred: list[str] = []
        self._visible: list[str] = []
        self.title: list[str] = []
        self.meta_description = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in {"article", "main"} and self._skip_depth == 0:
            self._preferred_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta" and self._skip_depth == 0:
            attributes = {key.lower(): value or "" for key, value in attrs}
            if attributes.get("name", "").lower() == "description":
                self.meta_description = attributes.get("content", "")
        if tag in self._BLOCK and self._skip_depth == 0:
            self._visible.append("\n")
            if self._preferred_depth:
                self._preferred.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._BLOCK and self._skip_depth == 0:
            self._visible.append("\n")
            if self._preferred_depth:
                self._preferred.append("\n")
        if tag == "title":
            self._in_title = False
        if tag in {"article", "main"} and self._preferred_depth:
            self._preferred_depth -= 1
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title.append(data)
        self._visible.append(data)
        if self._preferred_depth:
            self._preferred.append(data)

    def text(self) -> str:
        preferred = _normalise_text("".join(self._preferred))
        visible = _normalise_text("".join(self._visible))
        body = preferred or visible
        if not body:
            body = _normalise_text(" ".join(("".join(self.title), self.meta_description)))
        return body


def _fallback_extract(html: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not abort safe fallback
        return _normalise_text(re.sub(r"<[^>]+>", " ", html))
    return parser.text()


def extract_source_credits(raw_html: str) -> list[str]:
    """Keep visible source labels that article readability extraction can omit."""
    parser = _VisibleTextParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return []
    visible = _normalise_text("".join(parser._visible))
    credits = re.findall(r"(?:^|\n)(来源\s*[：:]\s*[^\n]{1,160})", visible)
    return list(dict.fromkeys(credits))[:6]


def _extract_text(value: str, *, is_html: bool) -> str:
    if not is_html:
        return _normalise_text(value)
    try:
        text = trafilatura.extract(value, include_comments=False, include_tables=True) or ""
    except Exception:  # noqa: BLE001 - parser errors are handled by the stdlib fallback
        text = ""
    return _normalise_text(text) or _fallback_extract(value)


def _retry_after(headers: Mapping[str, str], *, cap: float) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), cap)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            seconds = target.timestamp() - datetime.now(UTC).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        return min(max(seconds, 0.0), cap)


class BuiltinFetchProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_bytes: int = 5 * 1024 * 1024,
        allow_proxy_fake_ip: bool = False,
        max_redirects: int = 5,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        max_backoff: float = 5.0,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(30, connect=10),
            headers={
                "User-Agent": "YuQingAgent/0.3 (+public evidence research)",
                "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.8,*/*;q=0.1",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Accept-Encoding": "gzip, deflate",
            },
            follow_redirects=False,
        )
        self.max_bytes = max_bytes
        self.allow_proxy_fake_ip = allow_proxy_fake_ip
        self.max_redirects = max(0, max_redirects)
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.0, backoff_base)
        self.max_backoff = max(0.0, max_backoff)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _validate(self, url: str) -> str:
        return validate_public_url(url, allow_proxy_fake_ip=self.allow_proxy_fake_ip)

    async def _wait_before_retry(self, retry_number: int, headers: Mapping[str, str]) -> None:
        retry_after = _retry_after(headers, cap=self.max_backoff)
        delay = (
            retry_after
            if retry_after is not None
            else min(self.backoff_base * (2**retry_number), self.max_backoff)
        )
        if delay > 0:
            await asyncio.sleep(delay)

    async def fetch(self, url: str) -> FetchResult:
        current = self._validate(url)
        redirects = 0
        retries = 0

        while True:
            try:
                async with self.client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        target = response.headers.get("location")
                        if not target:
                            raise FetchError("重定向缺少 Location", kind="redirect")
                        redirects += 1
                        if redirects > self.max_redirects:
                            raise FetchError("重定向次数过多", kind="redirect")
                        # Resolve relative redirects using the already validated
                        # response URL, then validate the new host before fetching.
                        current = self._validate(urljoin(str(response.url), target))
                        continue

                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        if retries < self.max_retries:
                            await self._wait_before_retry(retries, response.headers)
                            retries += 1
                            continue
                        raise FetchError(
                            f"上游返回可重试 HTTP 状态 {response.status_code}，已耗尽重试次数",
                            kind="retry_exhausted",
                            retryable=True,
                            retry_exhausted=True,
                            status_code=response.status_code,
                        )
                    if response.status_code >= 400:
                        raise FetchError(
                            f"上游返回 HTTP 状态 {response.status_code}",
                            kind="http_status",
                            status_code=response.status_code,
                        )
                    if response.status_code < 200:
                        raise FetchError(
                            f"上游返回异常 HTTP 状态 {response.status_code}",
                            kind="http_status",
                            status_code=response.status_code,
                        )

                    content_type_header = response.headers.get("content-type", "")
                    content_type = content_type_header.split(";", 1)[0].strip().lower()
                    if (
                        content_type
                        not in _HTML_CONTENT_TYPES | _TEXT_CONTENT_TYPES | _PDF_CONTENT_TYPES
                    ):
                        raise FetchError(
                            f"不支持的 Content-Type: {content_type or '未声明'}",
                            kind="content_type",
                        )
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > self.max_bytes:
                                raise FetchError("页面超过 5MB 上限", kind="too_large")
                        except ValueError:
                            pass
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > self.max_bytes:
                            raise FetchError("页面超过 5MB 上限", kind="too_large")
                    raw = bytes(payload)

                    if content_type in _PDF_CONTENT_TYPES:
                        return self._parse_pdf(
                            raw, url=str(response.url), content_type=content_type
                        )

                    text_value = _decode_payload(raw, content_type=content_type_header)
                    if _looks_like_access_challenge(text_value):
                        raise FetchError(
                            "页面要求登录或人机验证，系统不会尝试绕过",
                            kind="challenge",
                        )
                    content_text = _extract_text(
                        text_value,
                        is_html=content_type in _HTML_CONTENT_TYPES,
                    )
                    if not content_text:
                        raise FetchError("未能抽取有效正文", kind="empty")
                    return FetchResult(
                        url=str(response.url),
                        html=text_value,
                        content_text=content_text,
                        content_type=content_type,
                    )
            except FetchError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if retries < self.max_retries:
                    await self._wait_before_retry(retries, {})
                    retries += 1
                    continue
                raise FetchError(
                    f"网络请求失败，已耗尽重试次数：{type(exc).__name__}",
                    kind="retry_exhausted",
                    retryable=True,
                    retry_exhausted=True,
                ) from exc
            except httpx.HTTPError as exc:
                # Non-network httpx failures (for example malformed request
                # URLs from a custom transport) are not safe to retry blindly.
                raise FetchError(f"抓取请求失败：{type(exc).__name__}", kind="request") from exc

    @staticmethod
    def _parse_pdf(raw: bytes, *, url: str, content_type: str) -> FetchResult:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FetchError(
                "暂不支持 PDF：未安装 pypdf 文本解析依赖",
                kind="pdf_unsupported",
            ) from exc
        try:
            reader = PdfReader(io.BytesIO(raw))
            text = _normalise_text("\n".join(page.extract_text() or "" for page in reader.pages))
        except Exception as exc:  # noqa: BLE001 - provider/parser detail is not user-safe
            raise FetchError("PDF 文本解析失败", kind="pdf_parse") from exc
        if not text:
            raise FetchError("PDF 未能抽取有效正文", kind="empty")
        return FetchResult(url=url, html="", content_text=text, content_type=content_type)
