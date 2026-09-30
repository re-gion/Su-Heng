from __future__ import annotations

import asyncio
from collections.abc import Sequence

from yuqing.core.fetch.base import FetchError, FetchProvider, FetchResult
from yuqing.core.fetch.builtin import BuiltinFetchProvider

__all__ = ["FetchChain"]


class FetchChain:
    """Conservative two-stage original-page fetch chain.

    Builtin fetching remains the default and cheapest path. Firecrawl is
    consulted only for a challenge page, an empty extraction, or a builtin
    failure after its finite retry budget has been exhausted. Security and
    permanent HTTP failures are deliberately not hidden by a fallback.
    """

    def __init__(
        self,
        builtin: FetchProvider | Sequence[FetchProvider] | None = None,
        firecrawl: FetchProvider | None = None,
        *,
        fallback: FetchProvider | None = None,
        providers: Sequence[FetchProvider] | None = None,
        max_fallback_calls: int = 5,
    ) -> None:
        if providers is not None:
            if builtin is not None or firecrawl is not None or fallback is not None:
                raise TypeError("providers 不能与单独 provider 参数同时使用")
            configured = list(providers)
        elif isinstance(builtin, Sequence) and not isinstance(builtin, (str, bytes)):
            if firecrawl is not None or fallback is not None:
                raise TypeError("provider 序列不能与 fallback 参数同时使用")
            configured = list(builtin)
        else:
            first = builtin or BuiltinFetchProvider()
            second = firecrawl or fallback
            configured = [first] + ([second] if second is not None else [])
        if not configured or len(configured) > 2:
            raise ValueError("FetchChain 需要 builtin provider，以及可选的一个 Firecrawl fallback")
        self.providers = configured
        self.max_fallback_calls = max(0, max_fallback_calls)
        self._fallback_calls = 0
        self._fallback_lock = asyncio.Lock()

    @property
    def builtin(self) -> FetchProvider:
        return self.providers[0]

    @property
    def firecrawl(self) -> FetchProvider | None:
        return self.providers[1] if len(self.providers) > 1 else None

    @staticmethod
    def _fallback_allowed(error: FetchError) -> bool:
        if (
            error.kind
            in {
                "challenge",
                "empty",
                "retry_exhausted",
                "pdf_unsupported",
                "pdf_parse",
            }
            or error.retry_exhausted
        ):
            return True
        # Keep compatibility with lightweight/custom providers that still
        # raise the old unclassified FetchError form.
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "验证码",
                "人机验证",
                "challenge",
                "captcha",
                "空正文",
                "未能抽取有效正文",
                "retry exhausted",
                "耗尽重试",
            )
        )

    async def fetch(self, url: str, *, allow_fallback: bool = False) -> FetchResult:
        try:
            result = await self.builtin.fetch(url)
            if self._empty_shell(result.content_text):
                first_error = FetchError("目标页面未返回可用正文", kind="empty")
            else:
                return result
        except FetchError as error:
            first_error = error
            if not self._fallback_allowed(error):
                raise

        fallback = self.firecrawl
        if fallback is None or not allow_fallback:
            raise first_error
        async with self._fallback_lock:
            if self._fallback_calls >= self.max_fallback_calls:
                raise FetchError(
                    f"本任务 Firecrawl 回退已达 {self.max_fallback_calls} 页上限",
                    kind="fallback_budget",
                ) from first_error
            self._fallback_calls += 1
        try:
            result = await fallback.fetch(url)
        except FetchError as error:
            # Keep the fallback's classification for callers and preserve the
            # builtin failure as exception context for diagnostics.
            raise error from first_error
        if self._empty_shell(result.content_text):
            raise FetchError("Firecrawl 返回空正文", kind="empty") from first_error
        return result

    @staticmethod
    def _empty_shell(content_text: str) -> bool:
        text = content_text.strip()
        if not text:
            return True
        return len(text) < 100 and any(
            marker in text
            for marker in (
                "下载央视新闻客户端",
                "请下载客户端查看",
                "打开客户端查看",
                "请在客户端查看",
            )
        )

    async def aclose(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
