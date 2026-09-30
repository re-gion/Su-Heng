from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from urllib.parse import urlsplit

from yuqing.core.llm.gateway import upstream_diagnostic
from yuqing.core.search.base import SearchParams, SearchProvider, SearchResult

SearchAcceptance = bool | tuple[bool, Sequence[str]]


class SearchChainExhausted(RuntimeError):
    def __init__(self, message, *, provider_diagnostics=None):
        super().__init__(message)
        self.provider_diagnostics = provider_diagnostics or []


class SearchChain:
    name = "search_chain"
    capabilities = {"freshness", "domain_filter", "publish_time"}
    domestic_names = ("langsearch", "exa", "qianfan", "bocha", "zhipu")
    foreign_names = ("exa", "tavily", "serper")

    def __init__(
        self,
        providers: Sequence[SearchProvider],
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 15,
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
        self._last_continued_from: ContextVar[str | None] = ContextVar(
            "last_continued_from", default=None
        )
        self._last_diagnostics: ContextVar[list[dict[str, object]] | None] = ContextVar(
            "last_search_diagnostics", default=None
        )

    @property
    def last_provider(self) -> str | None:
        return self._last_provider.get()

    @property
    def last_degraded_from(self) -> str | None:
        return self._last_degraded_from.get()

    @property
    def last_continued_from(self) -> str | None:
        """First provider consulted when routing continued without a provider failure."""

        return self._last_continued_from.get()

    @property
    def last_diagnostics(self) -> list[dict[str, object]]:
        """Diagnostics for each provider considered by the last search."""

        return list(self._last_diagnostics.get() or [])

    @property
    def provider_diagnostics(self) -> list[dict[str, object]]:
        """Compatibility alias for callers exposing provider diagnostics."""

        return self.last_diagnostics

    def _is_open(self, name: str) -> bool:
        opened = self._opened_at.get(name)
        if opened is None:
            return False
        if time.monotonic() - opened >= self.cooldown_seconds:
            self._opened_at.pop(name, None)
            self._failures[name] = 0
            return False
        return True

    def providers_for(self, params: SearchParams) -> list[SearchProvider]:
        preferred = (
            self.domestic_names
            if params.lang.split("-", 1)[0].lower() == "zh"
            else self.foreign_names
        )
        by_name = {provider.name: provider for provider in self.providers}
        selected = [by_name[name] for name in preferred if name in by_name]
        return selected or list(self.providers)

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
        return await self._search(params)

    async def search_filtered(
        self,
        params: SearchParams,
        accept: Callable[[SearchResult], SearchAcceptance],
        *,
        before_call: Callable[[], Awaitable[bool]] | None = None,
        min_source_groups: int = 1,
        source_group: Callable[[SearchResult], str] | None = None,
    ) -> list[SearchResult]:
        """Fall through providers until the caller's semantic gate accepts results.

        Provider-level domain filtering is not enough for an investigation: a search
        backend can return pages from the correct site or institution that are about a
        different event.  Keeping this fallback in the chain preserves breaker and
        provider-order behavior while letting the domain service own relevance.
        A predicate may return (accepted, reasons) for per-attempt diagnostics.
        """

        return await self._search(
            params,
            accept=accept,
            before_call=before_call,
            min_source_groups=min_source_groups,
            source_group=source_group,
        )

    @staticmethod
    def _group_for(item: SearchResult, source_group: Callable[[SearchResult], str] | None) -> str:
        if source_group is not None:
            group = source_group(item).strip()
            if group:
                return group
        return (urlsplit(item.url).hostname or item.url).lower().removeprefix("www.")

    @classmethod
    def _merge_diverse(
        cls,
        first: list[SearchResult],
        second: list[SearchResult],
        *,
        top_k: int,
        source_group: Callable[[SearchResult], str] | None,
    ) -> list[SearchResult]:
        unique: dict[str, SearchResult] = {}
        for item in [*first, *second]:
            url = urlsplit(item.url)._replace(fragment="").geturl().rstrip("/")
            previous = unique.get(url)
            if previous is None or (not previous.content_text and item.content_text):
                unique[url] = item
        ordered = list(unique.values())
        chosen: list[SearchResult] = []
        chosen_urls: set[str] = set()
        seen_groups: set[str] = set()
        for item in ordered:
            group = cls._group_for(item, source_group)
            if group not in seen_groups:
                chosen.append(item)
                chosen_urls.add(item.url)
                seen_groups.add(group)
        for item in ordered:
            if item.url not in chosen_urls:
                chosen.append(item)
        return chosen[:top_k]

    async def _search(
        self,
        params: SearchParams,
        *,
        accept: Callable[[SearchResult], SearchAcceptance] | None = None,
        before_call: Callable[[], Awaitable[bool]] | None = None,
        min_source_groups: int = 1,
        source_group: Callable[[SearchResult], str] | None = None,
    ) -> list[SearchResult]:
        errors: list[str] = []
        first_attempted: str | None = None
        last_successful: str | None = None
        candidate_results: list[SearchResult] = []
        last_candidate_provider: str | None = None
        diagnostics: list[dict[str, object]] = []
        self._last_diagnostics.set(diagnostics)
        self._last_provider.set(None)
        self._last_degraded_from.set(None)
        self._last_continued_from.set(None)
        required = "freshness" if params.freshness != "noLimit" else None
        eligible = self.providers_for(params)
        needed_groups = min(max(1, min_source_groups), params.top_k)
        check_coverage = accept is not None and needed_groups > 1
        runnable_count = sum(
            1 for provider in eligible if required is None or required in provider.capabilities
        )
        pending: list[tuple[SearchProvider, SearchParams, str | None]] = [
            (provider, params, None) for provider in eligible
        ]
        for index, (provider, params, recovery_reason) in enumerate(pending):
            rejected: dict[str, int] = {}

            def record(
                entry: dict[str, object],
                request=params,
                recovery=recovery_reason,
                reason_counts=rejected,
            ) -> None:
                diagnostics.append(
                    {
                        "query": request.query,
                        "freshness": request.freshness,
                        "recovery_reason": recovery,
                        "rejected": dict(reason_counts),
                        **entry,
                    }
                )

            def schedule_date_recovery(
                current_provider=provider,
                request=params,
                recovery=recovery_reason,
                position=index,
            ) -> None:
                # Only one extra request, through the same quota/budget/breaker
                # path. Never relax strict/recent windows, domains or relevance.
                if (
                    current_provider.name == "langsearch"
                    and accept is not None
                    and request.allow_freshness_fallback
                    and request.freshness[:1].isdigit()
                    and recovery is None
                ):
                    pending.insert(
                        position + 1,
                        (
                            current_provider,
                            request.model_copy(update={"freshness": "noLimit"}),
                            "relax_preferred_date_window",
                        ),
                    )

            if required and required not in provider.capabilities:
                record({"provider": provider.name, "status": "skipped", "reason": "capability"})
                continue
            if getattr(provider, "task_limit_reached", False):
                record({"provider": provider.name, "status": "skipped", "reason": "task_limit"})
                continue
            if self._is_open(provider.name):
                record({"provider": provider.name, "status": "skipped", "reason": "breaker_open"})
                continue
            first_attempted = first_attempted or provider.name
            attempts = (
                2 if runnable_count == 1 and not getattr(provider, "retry_managed", False) else 1
            )
            last_error: Exception | None = None
            for attempt in range(attempts):
                if before_call is not None and not await before_call():
                    record({"provider": provider.name, "status": "budget_exhausted", "attempts": 0})
                    self._last_provider.set(last_candidate_provider or last_successful)
                    return candidate_results
                try:
                    results = await provider.search(params)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(1)
            if last_error is not None:
                failure_reason = getattr(last_error, "kind", "provider_error")
                errors.append(f"{provider.name}: {type(last_error).__name__}")
                record(
                    {
                        "provider": provider.name,
                        "status": "error",
                        "attempts": attempts,
                        "error_type": type(last_error).__name__,
                        "error": upstream_diagnostic(last_error)["message"],
                        "diagnostic": upstream_diagnostic(last_error),
                        "reason": failure_reason,
                    }
                )
                # A local protection line is a routing policy, not evidence that the
                # upstream provider is unhealthy. Do not poison the circuit breaker.
                if failure_reason != "local_quota_guard":
                    self._failures[provider.name] += 1
                    if (
                        runnable_count > 1
                        and self._failures[provider.name] >= self.failure_threshold
                    ):
                        self._opened_at[provider.name] = time.monotonic()
                continue
            self._failures[provider.name] = 0
            try:
                if not isinstance(results, list):
                    raise TypeError("provider 返回值必须是 SearchResult 列表")
                if any(not isinstance(item, SearchResult) for item in results):
                    raise TypeError("provider 返回值包含非 SearchResult 项")
                before_filter = len(results)
                results = self._apply_domain_filters(results, params)
            except Exception as exc:
                errors.append(f"{provider.name}: {type(exc).__name__}")
                record(
                    {
                        "provider": provider.name,
                        "status": "invalid_response",
                        "error_type": type(exc).__name__,
                        "error": "搜索返回结构不合格",
                    }
                )
                self._failures[provider.name] += 1
                if runnable_count > 1 and self._failures[provider.name] >= self.failure_threshold:
                    self._opened_at[provider.name] = time.monotonic()
                continue
            last_successful = provider.name
            # Empty or entirely filtered results are not a successful answer;
            # continue in order to let the next provider cover the query.
            if not results:
                record(
                    {
                        "provider": provider.name,
                        "status": "filtered_empty" if before_filter else "empty",
                        "count": before_filter,
                        "accepted_count": 0,
                    }
                )
                schedule_date_recovery()
                continue
            scoped_count = len(results)
            if accept is not None:
                accepted_results = []
                for item in results:
                    decision = accept(item)
                    accepted, reasons = decision if isinstance(decision, tuple) else (decision, ())
                    if accepted:
                        accepted_results.append(item)
                    else:
                        for reason in dict.fromkeys(reasons):
                            rejected[reason] = rejected.get(reason, 0) + 1
                results = accepted_results
            if not results:
                record(
                    {
                        "provider": provider.name,
                        "status": "relevance_filtered_empty",
                        "count": before_filter,
                        "domain_accepted_count": scoped_count,
                        "accepted_count": 0,
                    }
                )
                schedule_date_recovery()
                continue
            accepted_count = len(results)
            if candidate_results:
                results = self._merge_diverse(
                    candidate_results,
                    results,
                    top_k=params.top_k,
                    source_group=source_group,
                )
            if check_coverage:
                groups = {self._group_for(item, source_group) for item in results}
                if len(groups) < needed_groups:
                    candidate_results = results
                    last_candidate_provider = provider.name
                    record(
                        {
                            "provider": provider.name,
                            "status": "insufficient_coverage",
                            "count": before_filter,
                            "domain_accepted_count": scoped_count,
                            "accepted_count": accepted_count,
                            "returned_count": len(results),
                            "source_groups": len(groups),
                        }
                    )
                    continue
            record(
                {
                    "provider": provider.name,
                    "status": "success",
                    "count": before_filter,
                    "domain_accepted_count": scoped_count,
                    "accepted_count": accepted_count,
                    "returned_count": len(results),
                }
            )
            self._last_provider.set(provider.name)
            prior = diagnostics[:-1]
            failed_provider = next(
                (
                    str(item["provider"])
                    for item in prior
                    if (
                        item.get("status") in {"error", "invalid_response"}
                        and item.get("reason") != "local_quota_guard"
                    )
                    or (item.get("status") == "skipped" and item.get("reason") == "breaker_open")
                ),
                None,
            )
            self._last_degraded_from.set(failed_provider)
            self._last_continued_from.set(
                first_attempted if first_attempted and first_attempted != provider.name else None
            )
            return results
        if last_successful is not None:
            returned_provider = last_candidate_provider or last_successful
            self._last_provider.set(returned_provider)
            failed_provider = next(
                (
                    str(item["provider"])
                    for item in diagnostics
                    if (
                        item.get("status") in {"error", "invalid_response"}
                        and item.get("reason") != "local_quota_guard"
                    )
                    or (item.get("status") == "skipped" and item.get("reason") == "breaker_open")
                ),
                None,
            )
            self._last_degraded_from.set(failed_provider)
            self._last_continued_from.set(
                first_attempted
                if first_attempted and first_attempted != returned_provider
                else None
            )
            return candidate_results
        raise SearchChainExhausted(
            "搜索链全部不可用：" + "; ".join(errors or ["无匹配能力的 provider"]),
            provider_diagnostics=diagnostics,
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
