from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from contextvars import ContextVar
from urllib.parse import urlsplit

from yuqing.core.search.base import SearchParams, SearchProvider, SearchResult


class SearchChainExhausted(RuntimeError):
    pass


class SearchChain:
    name = "search_chain"
    capabilities = {"freshness", "domain_filter", "publish_time"}

    def __init__(
        self,
        providers: Sequence[SearchProvider],
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 60,
        failures: dict[str, int] | None = None,
        opened_at: dict[str, float] | None = None,
    ):
        if not providers:
            raise ValueError("搜索链至少需要一个 provider")
        self.providers = list(providers)
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = cooldown_seconds
        self._failures = failures if failures is not None else {}
        self._opened_at = opened_at if opened_at is not None else {}
        for provider in providers:
            self._failures.setdefault(provider.name, 0)
        self._last_provider: ContextVar[str | None] = ContextVar("last_provider", default=None)
        self._last_degraded_from: ContextVar[str | None] = ContextVar(
            "last_degraded_from", default=None
        )

    @property
    def last_provider(self) -> str | None:
        return self._last_provider.get()

    @property
    def last_degraded_from(self) -> str | None:
        return self._last_degraded_from.get()

    def _is_open(self, name: str) -> bool:
        opened = self._opened_at.get(name)
        if opened is None:
            return False
        if time.monotonic() - opened >= self.cooldown_seconds:
            self._opened_at.pop(name, None)
            self._failures[name] = 0
            return False
        return True

    @staticmethod
    def _apply_domain_filters(
        results: list[SearchResult], params: SearchParams
    ) -> list[SearchResult]:
        include = {item.lower().lstrip(".") for item in params.include_domains}
        exclude = {item.lower().lstrip(".") for item in params.exclude_domains}

        def matches(host: str, domains: set[str]) -> bool:
            return any(host == domain or host.endswith(f".{domain}") for domain in domains)

        filtered: list[SearchResult] = []
        for result in results:
            host = (urlsplit(result.url).hostname or "").lower()
            if include and not matches(host, include):
                continue
            if exclude and matches(host, exclude):
                continue
            filtered.append(result)
        return filtered[: params.top_k]

    async def search(self, params: SearchParams) -> list[SearchResult]:
        errors: list[str] = []
        first_attempted: str | None = None
        last_successful: str | None = None
        required = "freshness" if params.freshness != "noLimit" else None
        eligible = [
            provider
            for provider in self.providers
            if required is None or required in provider.capabilities
        ]
        if params.lang.split("-", 1)[0].lower() != "zh":
            preferred = {"tavily": 0, "serper": 1}
            eligible.sort(key=lambda provider: preferred.get(provider.name, 2))
        for provider in eligible:
            if required and required not in provider.capabilities:
                continue
            if self._is_open(provider.name):
                continue
            first_attempted = first_attempted or provider.name
            attempts = (
                2 if len(eligible) == 1 and not getattr(provider, "retry_managed", False) else 1
            )
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    results = await provider.search(params)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(1)
            if last_error is not None:
                errors.append(
                    f"{provider.name}: {type(last_error).__name__}: {str(last_error)[:160]}"
                )
                self._failures[provider.name] += 1
                if len(eligible) > 1 and self._failures[provider.name] >= self.failure_threshold:
                    self._opened_at[provider.name] = time.monotonic()
                continue
            self._failures[provider.name] = 0
            last_successful = provider.name
            results = self._apply_domain_filters(results, params)
            # 部分上游把 include_domains 当提示而非约束。域名过滤后为空时，
            # 继续尝试下一 provider，避免把站外结果误当成目标平台帖子。
            if (params.include_domains or params.exclude_domains) and not results:
                continue
            self._last_provider.set(provider.name)
            self._last_degraded_from.set(
                first_attempted if first_attempted and first_attempted != provider.name else None
            )
            return results
        if last_successful is not None:
            self._last_provider.set(last_successful)
            self._last_degraded_from.set(
                first_attempted if first_attempted and first_attempted != last_successful else None
            )
            return []
        raise SearchChainExhausted(
            "搜索链全部不可用：" + "; ".join(errors or ["无匹配能力的 provider"])
        )

    def statuses(self) -> list[dict[str, object]]:
        return [
            {
                "name": provider.name,
                "configured": True,
                "breaker": "open" if self._is_open(provider.name) else "closed",
                "failures": self._failures[provider.name],
            }
            for provider in self.providers
        ]
