from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from yuqing.core.fetch.base import FetchProvider, FetchResult
from yuqing.core.search.base import SearchParams, SearchProvider, SearchResult
from yuqing.storage.db import Database


class ProviderQuotaExceeded(RuntimeError):
    """The local safety budget protected a renewable or one-time free pool."""

    kind = "local_quota_guard"


class ProviderRateLimiter:
    """Small shared interval limiter for provider account-level QPS."""

    def __init__(self, interval: float):
        self.interval = max(0.0, interval)
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            delay = self.interval - (time.monotonic() - self._last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = time.monotonic()


@dataclass(frozen=True)
class QuotaWindow:
    period: str
    normal_limit: int
    critical_limit: int
    unit: str = "calls"


POLICIES: dict[str, tuple[QuotaWindow, ...]] = {
    # Public documentation conflicts between about 50/day and 100/day. Keep
    # the smaller dedicated-Web-Search allowance until the account console is checked.
    "qianfan": (
        QuotaWindow("day", 40, 50),
        QuotaWindow("month", 1200, 1500),
    ),
    # Trial/experience pool is not renewable. Preserve 20% for important gaps.
    "bocha": (QuotaWindow("lifetime", 800, 1000),),
    "tavily": (QuotaWindow("month", 800, 1000, unit="credits"),),
    "serper": (QuotaWindow("lifetime", 2000, 2500),),
}


def _period_key(period: str, now: datetime) -> str:
    if period == "day":
        return f"day:{now.date().isoformat()}"
    if period == "month":
        return f"month:{now:%Y-%m}"
    return "lifetime"


def _amount(provider: str, params: SearchParams) -> int:
    if provider == "exa":
        return 7 + min(params.top_k, 10)
    return 1


class ProviderQuotaManager:
    def __init__(self, database: Database):
        self.database = database

    async def reserve(
        self,
        provider: str,
        params: SearchParams,
        *,
        critical: bool = False,
        task_id: str | None = None,
        task_limit: int | None = None,
    ) -> None:
        policy = POLICIES.get(provider, ())
        now = datetime.now().astimezone()
        amount = _amount(provider, params)
        entries = [
            (
                f"search:{provider}:{window.unit}",
                _period_key(window.period, now),
                window.critical_limit if critical else window.normal_limit,
                amount,
            )
            for window in policy
        ]
        if task_id is not None and task_limit is not None:
            entries.append((f"search:{provider}:task_calls", f"task:{task_id}", task_limit, 1))
        if not await self.database.consume_quota_bundle(entries):
            pool = "应急" if critical else "常规"
            raise ProviderQuotaExceeded(f"{provider} 本地{pool}额度保护线已到达")
        if provider == "exa":
            # Keep the existing estimate and counter, without assuming an account balance.
            await self.database.record_provider_usage(
                "search:exa:milli_usd", _period_key("month", now), amount
            )
        for period in ("day", "month"):
            await self.database.record_provider_usage(
                f"search:{provider}:observed_calls", _period_key(period, now), 1
            )

    async def record_unbounded_usage(self, provider: str, usage: dict[str, Any]) -> None:
        if provider != "langsearch":
            return
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if tokens <= 0:
            return
        now = datetime.now().astimezone()
        for period in ("day", "month"):
            await self.database.record_provider_usage(
                "search:langsearch:tokens", _period_key(period, now), tokens
            )

    async def public_status(self) -> dict[str, dict[str, Any]]:
        """Return local protection counters without presenting them as provider billing facts."""

        now = datetime.now().astimezone()
        today = _period_key("day", now)
        month = _period_key("month", now)
        langsearch_used = await self.database.provider_usage("search:langsearch:tokens", today)
        statuses: dict[str, dict[str, Any]] = {
            "exa": {
                "state": "metered_without_fixed_limit",
                "upstream_quota_verified": False,
                "activity": await self._activity("exa", today, month),
                "windows": [
                    {
                        "period": "month",
                        "period_key": month,
                        "unit": "milli_usd",
                        "used": await self.database.provider_usage("search:exa:milli_usd", month),
                        "normal_limit": None,
                        "critical_limit": None,
                        "normal_remaining": None,
                        "critical_remaining": None,
                    }
                ],
            },
            "langsearch": {
                "state": "metered_without_fixed_limit",
                "upstream_quota_verified": False,
                "activity": await self._activity("langsearch", today, month),
                "windows": [
                    {
                        "period": "day",
                        "period_key": today,
                        "unit": "tokens",
                        "used": langsearch_used,
                        "normal_limit": None,
                        "critical_limit": None,
                        "normal_remaining": None,
                        "critical_remaining": None,
                    }
                ],
            },
        }
        for provider, policy in POLICIES.items():
            windows: list[dict[str, Any]] = []
            normal_limit_reached = False
            critical_limit_reached = False
            for window in policy:
                period_key = _period_key(window.period, now)
                used = await self.database.provider_usage(
                    f"search:{provider}:{window.unit}", period_key
                )
                normal_remaining = max(0, window.normal_limit - used)
                critical_remaining = max(0, window.critical_limit - used)
                normal_limit_reached = normal_limit_reached or normal_remaining == 0
                critical_limit_reached = critical_limit_reached or critical_remaining == 0
                windows.append(
                    {
                        "period": window.period,
                        "period_key": period_key,
                        "unit": window.unit,
                        "used": used,
                        "normal_limit": window.normal_limit,
                        "critical_limit": window.critical_limit,
                        "normal_remaining": normal_remaining,
                        "critical_remaining": critical_remaining,
                    }
                )
            state = (
                "critical_limit_reached"
                if critical_limit_reached
                else "normal_limit_reached"
                if normal_limit_reached
                else "available"
            )
            statuses[provider] = {
                "state": state,
                "upstream_quota_verified": False,
                "activity": await self._activity(provider, today, month),
                "windows": windows,
            }
        return statuses

    async def _activity(self, provider: str, today: str, month: str) -> dict[str, int]:
        scope = f"search:{provider}:observed_calls"
        return {
            "day_calls": await self.database.provider_usage(scope, today),
            "month_calls": await self.database.provider_usage(scope, month),
        }

    async def fetch_status(self) -> dict[str, Any]:
        now = datetime.now().astimezone()
        today, month = _period_key("day", now), _period_key("month", now)
        used = await self.database.provider_usage("fetch:firecrawl:credits", month)
        return {
            "state": "critical_limit_reached"
            if used >= 1000
            else "normal_limit_reached"
            if used >= 800
            else "available",
            "upstream_quota_verified": False,
            "activity": {
                "day_calls": await self.database.provider_usage(
                    "fetch:firecrawl:observed_calls", today
                ),
                "month_calls": await self.database.provider_usage(
                    "fetch:firecrawl:observed_calls", month
                ),
            },
            "windows": [
                {
                    "period": "month",
                    "period_key": month,
                    "unit": "credits",
                    "used": used,
                    "normal_limit": 800,
                    "critical_limit": 1000,
                    "normal_remaining": max(0, 800 - used),
                    "critical_remaining": max(0, 1000 - used),
                }
            ],
        }

    async def reserve_fetch(self, provider: str, *, critical: bool = False) -> None:
        if provider != "firecrawl":
            return
        now = datetime.now().astimezone()
        limit = 1000 if critical else 800
        allowed = await self.database.consume_quota_bundle(
            [("fetch:firecrawl:credits", _period_key("month", now), limit, 1)]
        )
        if not allowed:
            pool = "应急" if critical else "常规"
            raise ProviderQuotaExceeded(f"Firecrawl 本地{pool}额度保护线已到达")
        for period in ("day", "month"):
            await self.database.record_provider_usage(
                "fetch:firecrawl:observed_calls", _period_key(period, now), 1
            )


class QuotaAwareSearchProvider:
    """Provider decorator shared by normal search and topic discovery."""

    def __init__(
        self,
        provider: SearchProvider,
        quotas: ProviderQuotaManager,
        *,
        limiter: ProviderRateLimiter | None = None,
        max_calls_per_task: int | None = None,
    ):
        self.provider = provider
        self.quotas = quotas
        self.name = provider.name
        self.capabilities = provider.capabilities
        self.retry_managed = getattr(provider, "retry_managed", False)
        self.client = getattr(provider, "client", None)
        self.limiter = limiter
        self.max_calls_per_task = max_calls_per_task
        self._task_calls = 0
        self._task_id: str | None = None
        self._task_lock = asyncio.Lock()

    async def bind_task(self, task_id: str) -> None:
        """Restore the task request count when a paused run resumes."""

        async with self._task_lock:
            self._task_id = task_id
            if self.max_calls_per_task is not None:
                self._task_calls = await self.quotas.database.provider_usage(
                    f"search:{self.name}:task_calls", f"task:{task_id}"
                )

    @property
    def task_limit_reached(self) -> bool:
        return self.max_calls_per_task is not None and self._task_calls >= self.max_calls_per_task

    async def search(self, params: SearchParams) -> list[SearchResult]:
        critical = getattr(params, "priority", "normal") == "critical"
        async with self._task_lock:
            if self.task_limit_reached:
                raise ProviderQuotaExceeded(f"{self.name} 本任务请求上限已到达")
            await self.quotas.reserve(
                self.name,
                params,
                critical=critical,
                task_id=self._task_id,
                task_limit=self.max_calls_per_task,
            )
            self._task_calls += 1
        if self.limiter is not None:
            await self.limiter.acquire()
        results = await self.provider.search(params)
        usage = getattr(self.provider, "last_usage", {})
        if not usage and results:
            usage = results[0].usage or {}
        if isinstance(usage, dict):
            await self.quotas.record_unbounded_usage(self.name, usage)
        return results

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)


class QuotaAwareFetchProvider:
    def __init__(
        self,
        provider: FetchProvider,
        quotas: ProviderQuotaManager,
        *,
        name: str = "firecrawl",
        critical: bool = False,
    ):
        self.provider = provider
        self.quotas = quotas
        self.name = name
        self.critical = critical

    async def fetch(self, url: str) -> FetchResult:
        await self.quotas.reserve_fetch(self.name, critical=self.critical)
        return await self.provider.fetch(url)

    async def aclose(self) -> None:
        close = getattr(self.provider, "aclose", None)
        if close is not None:
            await close()


def unwrap_providers(providers: Sequence[SearchProvider]) -> list[SearchProvider]:
    return [getattr(provider, "provider", provider) for provider in providers]
