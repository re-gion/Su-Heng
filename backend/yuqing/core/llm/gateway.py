from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
import weakref
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import wraps
from types import SimpleNamespace
from typing import Any

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from tenacity import wait_exponential

from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.roles import LLMRole
from yuqing.core.text_safety import repair_unicode_scalars

_MODEL_SLOTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str, int], asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _model_slot(config: Any) -> asyncio.Semaphore:
    """Bound simultaneous calls sharing one endpoint and credential across tasks."""
    try:
        limit = min(16, max(1, int(os.getenv("YUQING_LLM_MAX_INFLIGHT", "2"))))
    except ValueError:
        limit = 2
    loop = asyncio.get_running_loop()
    slots = _MODEL_SLOTS.setdefault(loop, {})
    fingerprint = hashlib.sha256(config.api_key.encode("utf-8")).hexdigest()
    key = (config.base_url.rstrip("/").lower(), fingerprint, limit)
    return slots.setdefault(key, asyncio.Semaphore(limit))


# 推理模型会在可见正文前消耗 max_tokens 输出思考内容；预算过小时正文为空或截断。
# 命中长度截断时按倍数放大预算重试，封顶避免无界膨胀。
LENGTH_RETRY_GROWTH = 4
LENGTH_RETRY_MAX_TOKENS = 32768


class LLMOutputTruncated(RuntimeError):
    """The provider exhausted output tokens without a complete response."""


class LLMServiceUnavailable(RuntimeError):
    """Stop a task branch while the same model endpoint is repeatedly unavailable."""


class LLMBudgetExhausted(RuntimeError):
    """The local task or phase budget cannot reserve another model call."""

    def __init__(self, message="LLM token budget exhausted", *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


# 网络、截断与格式修复共用三次请求；原始上游响应不进入用户消息。
UPSTREAM_RETRY_ATTEMPTS = 3
UPSTREAM_RETRY_MAX_WAIT = 60

# 上游不可用（可恢复）的异常集合。重试策略与"该不该把这次失败记成材料问题"
# 共用同一份定义，避免两处判断漂移。
UPSTREAM_RETRY_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    json.JSONDecodeError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
)

_QUOTA_MARKERS = (
    "insufficient_quota",
    "billing_hard_limit",
    "credit_balance",
    "account_balance",
    "余额不足",
    "账户额度不足",
    "账户配额不足",
    "欠费",
)
_CONCURRENCY_MARKERS = (
    "concurrency_limit",
    "concurrent_request",
    "too_many_concurrent",
    "并发上限",
    "并发限制",
    "并发数",
)
_RATE_MARKERS = (
    "rate_limit",
    "requests_per_minute",
    "tokens_per_minute",
    "requests_per_second",
    "rate exceeded",
    "请求频率",
    "速率限制",
    "每分钟",
    "每秒",
    "tpm",
    "rpm",
    "rps",
)


def _retry_after_seconds(exc: RateLimitError) -> int | None:
    value = exc.response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            seconds = (stamp - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0, math.ceil(seconds))


def rate_limit_diagnostic(exc: RateLimitError) -> dict[str, str | int]:
    """Only persist a bounded category and retry delay, never an upstream body."""
    body = exc.body
    if not isinstance(body, dict):
        try:
            body = exc.response.json()
        except ValueError:
            body = {}
    detail = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    clues = " ".join(
        str(detail.get(key) or "")[:500].lower() for key in ("code", "type", "message")
    )
    if any(marker in clues for marker in _QUOTA_MARKERS):
        category = "account_quota"
    elif any(marker in clues for marker in _CONCURRENCY_MARKERS):
        category = "concurrency"
    elif any(marker in clues for marker in _RATE_MARKERS):
        category = "rate"
    else:
        category = "unknown"
    result: dict[str, str | int] = {"category": category}
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        result["retry_after_seconds"] = retry_after
    return result


def _retryable_upstream(exc: BaseException) -> bool:
    if not isinstance(exc, UPSTREAM_RETRY_EXCEPTIONS):
        return False
    if isinstance(exc, RateLimitError):
        detail = rate_limit_diagnostic(exc)
        if detail["category"] == "account_quota":
            return False
        if int(detail.get("retry_after_seconds", 0)) > UPSTREAM_RETRY_MAX_WAIT:
            return False
    return True


_exponential_wait = wait_exponential(multiplier=1, min=2, max=UPSTREAM_RETRY_MAX_WAIT)


def _wait_upstream_retry(retry_state: Any) -> float:
    delay = float(_exponential_wait(retry_state))
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RateLimitError):
        retry_after = rate_limit_diagnostic(exc).get("retry_after_seconds", 0)
        delay = max(delay, float(retry_after))
    return delay


def is_upstream_failure(exc: BaseException) -> bool:
    """上游服务/网关故障（换时间重试可能成功），区别于材料本身的语义问题。"""
    return isinstance(exc, (*UPSTREAM_RETRY_EXCEPTIONS, LLMServiceUnavailable)) or getattr(
        getattr(exc, "response", None), "status_code", None
    ) in {401, 402, 403}


def upstream_diagnostic(exc: BaseException, *, stage: str = "", batch: str = "") -> dict:
    """Persist safe classifications, never request text, credentials or response bodies."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    body = getattr(exc, "body", {})
    detail = body.get("error", body) if isinstance(body, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    clues = " ".join(str(detail.get(k, ""))[:1000].lower() for k in ("code", "type", "message"))
    category = "unknown"
    if isinstance(exc, LLMBudgetExhausted):
        category = "local_budget"
    elif getattr(exc, "provider_diagnostics", None) is not None:
        category = "search_exhausted"
    elif any(
        s in clues for s in ("context_length", "maximum context", "too many tokens", "too long")
    ):
        category = "request_length"
    elif any(s in clues for s in ("content_filter", "content_policy", "safety", "敏感")):
        category = "content_restriction"
    elif any(s in clues for s in ("response_format", "json", "unsupported", "invalid parameter")):
        category = "request_parameter"
    elif status == 429:
        category = rate_limit_diagnostic(exc)["category"]
    elif isinstance(exc, LLMServiceUnavailable):
        category = "upstream_unavailable"
    elif status in {401, 403}:
        category = "authentication"
    elif status and status >= 500:
        category = "upstream_unavailable"
    elif isinstance(exc, (APIConnectionError, APITimeoutError, TimeoutError, ConnectionError)):
        category = "connection"
    elif isinstance(exc, (ValueError, json.JSONDecodeError)):
        category = "invalid_output"
    elif isinstance(exc, LLMOutputTruncated):
        category = "output_truncated"
    code = str(detail.get("code") or "")
    if not re.fullmatch(
        r"(?:[0-9]{1,10}|[A-Za-z][A-Za-z_]{2,60})", code
    ) or code.lower().startswith(("sk_", "key_", "token_")):
        code = "unknown"
    labels = {
        "local_budget": "本地阶段预算不足，尚未发起上游请求",
        "authentication": "上游拒绝认证或访问权限",
        "search_exhausted": "搜索服务链未取得可用结果，详见逐服务诊断",
        "request_length": "请求超过模型长度限制",
        "content_restriction": "上游拒绝该内容",
        "request_parameter": "上游不接受请求参数或输出格式",
        "connection": "上游连接失败",
        "upstream_unavailable": "上游服务暂时不可用",
        "invalid_output": "返回内容未通过结构校验",
        "output_truncated": "模型输出达到长度上限，尚未取得完整内容",
        "unknown": "上游未提供可确认原因",
    }
    result = {
        "status": status,
        "code": code,
        "category": category,
        "message": labels.get(category, "上游额度或速率限制"),
        "error_type": type(exc).__name__,
        "stage": stage,
        "batch": batch,
    }
    if category == "connection":
        causes = []
        cause = exc
        while cause is not None and len(causes) < 6:
            causes.append(type(cause).__name__)
            for field in ("errno", "winerror"):
                if isinstance(getattr(cause, field, None), int):
                    result[field] = getattr(cause, field)
            cause = cause.__cause__
        result["connection_causes"] = causes
    if isinstance(exc, LLMBudgetExhausted):
        result["budget"] = exc.diagnostic
    if getattr(exc, "provider_diagnostics", None) is not None:
        result["providers"] = exc.provider_diagnostics
    return result


def sanitize_upstream_message(exc: BaseException) -> str:
    """带 HTTP 状态码的上游错误收敛为一句话（剥掉网关错误页等响应体原文）；其余异常原样保留。"""
    if isinstance(exc, RateLimitError):
        detail = rate_limit_diagnostic(exc)
        labels = {
            "account_quota": "账户余额或总额度不足",
            "concurrency": "并发请求达到上限",
            "rate": "请求速率达到上限",
            "unknown": "限流原因未指明，需查服务商调用日志",
        }
        message = f"上游模型服务返回 429（{labels[str(detail['category'])]}）"
        if "retry_after_seconds" in detail:
            message += f"，建议等待 {detail['retry_after_seconds']} 秒"
        return message
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        detail = upstream_diagnostic(exc)
        return f"上游服务返回 {status}（{detail['message']}；{type(exc).__name__}）"
    if getattr(exc, "provider_diagnostics", None) is not None:
        labels = {
            "capability": "缺少所需能力",
            "task_limit": "单任务调用限额已用完",
            "breaker_open": "失败熔断中",
            "local_quota_guard": "本机额度保护线",
            "provider_error": "服务调用失败",
        }
        parts = [
            f"{d['provider']}：{labels.get(d.get('reason'), d.get('error') or d.get('status', '原因未知'))}"
            for d in exc.provider_diagnostics
        ]
        return "搜索链不可用（" + "；".join(parts) + "）"
    return f"{type(exc).__name__}: {exc}"


def logical_model_call(stage):
    """Share the request allowance across caller and transport repair loops."""

    def decorate(method):
        @wraps(method)
        async def call(self, *args, **kwargs):
            gateway = self.gateway
            context = (
                gateway.logical_call(stage=stage)
                if hasattr(gateway, "logical_call")
                else nullcontext()
            )
            with context:
                return await method(self, *args, **kwargs)

        return call

    return decorate


class LLMGateway:
    def __init__(self, factory: LLMClientFactory):
        self.factory = factory
        self.tokens_used = 0
        self.calls = 0
        self.token_limit: int | None = None
        self.absolute_token_limit: int | None = None
        self._tokens_reserved = 0
        self._budget_lock = asyncio.Lock()
        self.record_call = None
        self._call_context = ContextVar("llm_call_context", default=None)
        self._logical_context = ContextVar("llm_logical_context", default=None)
        self._endpoint_failures = {}
        self._output_floors = {}

    def learn_output_budgets(self, records):
        """Reuse bounded numeric telemetry, never another task's content or decisions."""
        for record in records:
            output = record.get("output_tokens")
            reasoning = record.get("reasoning_tokens")
            if (
                record.get("status") != "complete"
                or not isinstance(output, int)
                or not isinstance(reasoning, int)
                or output <= 0
                or reasoning <= 0
            ):
                continue
            key = (record.get("model"), record.get("role"), record.get("stage"))
            # Leave space for variable reasoning without reserving the previous 4x retry cap.
            floor = min(LENGTH_RETRY_MAX_TOKENS, math.ceil(output * 1.25 / 1024) * 1024)
            self._output_floors[key] = max(self._output_floors.get(key, 0), floor)

    @contextmanager
    def context(self, **values):
        token = self._call_context.set({**(self._call_context.get() or {}), **values})
        try:
            yield
        finally:
            self._call_context.reset(token)

    @contextmanager
    def logical_call(self, ledger=None, **values):
        if (
            ledger is None
            and self._logical_context.get()
            and (self._call_context.get() or {}).get("stage") == values.get("stage")
        ):
            with self.context(**values):
                yield
            return
        token = self._logical_context.set(
            ledger if ledger is not None else {"call_id": uuid.uuid4().hex, "attempts": 0}
        )
        try:
            with self.context(**values):
                yield
        finally:
            self._logical_context.reset(token)

    def budget_diagnostic(self, required: int = 0) -> dict:
        return {
            "tokens_used": self.tokens_used,
            "tokens_reserved": self._tokens_reserved,
            "required_tokens": required,
            "phase_token_limit": self.token_limit,
            "total_token_limit": getattr(self, "total_token_limit", None),
        }

    async def wait_for_recovery(self, role: LLMRole) -> None:
        """Give mandatory final review one bounded cooldown after an optional branch fails."""
        config = self.factory.config(role)
        endpoint = (config.base_url, hashlib.sha256(config.api_key.encode()).hexdigest())
        _, until = self._endpoint_failures.get(endpoint, (0, 0))
        delay = min(30.0, max(0.0, until - time.monotonic()))
        if delay:
            await asyncio.sleep(delay)

    async def complete_json(
        self, role: LLMRole, system: str, user: str, *, max_tokens: int = 2000
    ) -> dict[str, Any]:
        # JSON mode's format contract must not depend on user-supplied comments.
        system = system + "\n只输出一个有效的 JSON 对象。"
        config = self.factory.config(role)
        endpoint = (config.base_url, hashlib.sha256(config.api_key.encode()).hexdigest())
        failures, until = self._endpoint_failures.get(endpoint, (0, 0))
        if until > time.monotonic():
            raise LLMServiceUnavailable(
                "同一模型服务连续连接或服务器故障，当前分支暂止；已保存内容可恢复"
            )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        ledger = self._logical_context.get() or {"call_id": uuid.uuid4().hex, "attempts": 0}
        call_id = ledger["call_id"]
        output_key = (config.model, role, (self._call_context.get() or {}).get("stage", role))
        budget = min(
            max(max_tokens, self._output_floors.get(output_key, 0)), LENGTH_RETRY_MAX_TOKENS
        )
        truncated = False
        if ledger["attempts"] >= UPSTREAM_RETRY_ATTEMPTS:
            raise ValueError("逻辑模型调用已达到三次实际请求上限")
        for _ in range(UPSTREAM_RETRY_ATTEMPTS - ledger["attempts"]):
            attempt = ledger["attempts"] + 1
            reservation = len(system) + len(user) + budget
            async with self._budget_lock:
                caps = [v for v in (self.token_limit, self.absolute_token_limit) if v is not None]
                if caps and attempt == 1:
                    available = (
                        min(caps)
                        - self.tokens_used
                        - self._tokens_reserved
                        - len(system)
                        - len(user)
                    )
                    # A learned headroom must not reject a call that still fits its
                    # caller-specified budget. Never exceed the original phase cap.
                    if available >= min(max_tokens, LENGTH_RETRY_MAX_TOKENS):
                        budget = min(budget, available)
                        reservation = len(system) + len(user) + budget
                if caps and self.tokens_used + self._tokens_reserved + reservation > min(caps):
                    raise LLMBudgetExhausted(diagnostic=self.budget_diagnostic(reservation))
                self._tokens_reserved += reservation
            record = {
                "phase": getattr(self, "phase", "unknown"),
                "stage": role,
                **(self._call_context.get() or {}),
                "call_id": call_id,
                "attempt": attempt,
                "role": role,
                "model": config.model,
                "max_output_tokens": budget,
                "reservation_tokens": reservation,
                "input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 0,
                "metering_source": "unavailable",
                "finish_reason": None,
                "request_id": None,
                "queue_ms": 0,
                "request_ms": 0,
                "started_at": datetime.now(UTC).isoformat(),
            }
            response = None
            error = None
            queued_at = time.monotonic()
            requested_at = None
            try:
                if self.record_call is not None:
                    await self.record_call({**record, "status": "queued"})
                async with _model_slot(config):
                    requested_at = time.monotonic()
                    record["requested_at"] = datetime.now(UTC).isoformat()
                    record["queue_ms"] = round((requested_at - queued_at) * 1000, 2)
                    self.calls += 1
                    ledger["attempts"] = attempt
                    if self.record_call is not None:
                        await self.record_call({**record, "status": "inflight"})
                    response = await self.factory.get(role).chat.completions.create(
                        model=config.model,
                        messages=messages,
                        temperature=config.temperature,
                        max_tokens=budget,
                        response_format={"type": "json_object"},
                    )
                usage = response.usage
                record.update(
                    input_tokens=getattr(usage, "prompt_tokens", None),
                    output_tokens=getattr(usage, "completion_tokens", None),
                    reasoning_tokens=getattr(
                        getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
                    ),
                    total_tokens=usage.total_tokens
                    if usage and isinstance(usage.total_tokens, int)
                    else reservation,
                    metering_source="provider"
                    if usage and isinstance(usage.total_tokens, int)
                    else "estimate",
                    finish_reason=getattr(response.choices[0], "finish_reason", None)
                    if response.choices
                    else None,
                    request_id=self._safe_request_id(
                        getattr(response, "_request_id", None) or getattr(response, "id", None)
                    ),
                )
                async with self._budget_lock:
                    self.tokens_used += record["total_tokens"]
                if record["finish_reason"] == "length":
                    truncated = True
                    raise LLMOutputTruncated("模型输出达到长度上限，未得到完整 JSON")
                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("模型没有返回 JSON 正文")
                content = response.choices[0].message.content.strip().lstrip("\ufeff")
                # Only remove a complete outer Markdown fence. Never extract a guessed
                # object from prose, complete missing fields, or rewrite quoted evidence.
                fenced = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", content, re.DOTALL)
                if fenced:
                    content = fenced.group(1)
                    record["format_recovery"] = "outer_json_fence"
                try:
                    result = json.loads(content)
                except json.JSONDecodeError as exc:
                    record["json_error"] = {
                        "line": exc.lineno,
                        "column": exc.colno,
                        "position": exc.pos,
                        "characters": len(content),
                    }
                    raise
                if not isinstance(result, dict):
                    raise ValueError("模型返回的 JSON 不是对象")
                self._endpoint_failures.pop(endpoint, None)
                self.learn_output_budgets([{**record, "status": "complete"}])
                if truncated and record["reasoning_tokens"] and record["output_tokens"] is None:
                    self._output_floors[output_key] = budget
                return repair_unicode_scalars(result)
            except asyncio.CancelledError as exc:
                error = exc
                raise
            except Exception as exc:
                error = exc
                record["request_id"] = self._safe_request_id(getattr(exc, "request_id", None))
                record["diagnostic"] = upstream_diagnostic(exc, stage=record.get("stage", ""))
                if record["diagnostic"]["category"] in {"connection", "upstream_unavailable"}:
                    failures, _ = self._endpoint_failures.get(endpoint, (0, 0))
                    failures += 1
                    self._endpoint_failures[endpoint] = (
                        failures,
                        time.monotonic() + 30 if failures >= 3 else 0,
                    )
            finally:
                if requested_at is not None:
                    record["request_ms"] = round((time.monotonic() - requested_at) * 1000, 2)
                else:
                    record["queue_ms"] = round((time.monotonic() - queued_at) * 1000, 2)
                record["status"] = (
                    "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "failed"
                    if error
                    else "complete"
                )
                record["finished_at"] = datetime.now(UTC).isoformat()
                async with self._budget_lock:
                    self._tokens_reserved -= reservation
                if self.record_call is not None:
                    await self.record_call(record)
            if isinstance(error, LLMOutputTruncated):
                if budget >= LENGTH_RETRY_MAX_TOKENS or attempt == UPSTREAM_RETRY_ATTEMPTS:
                    raise error
                budget = min(budget * LENGTH_RETRY_GROWTH, LENGTH_RETRY_MAX_TOKENS)
            elif (
                not (_retryable_upstream(error) or isinstance(error, ValueError))
                or attempt == UPSTREAM_RETRY_ATTEMPTS
            ):
                raise error
            elif not isinstance(error, ValueError):
                await asyncio.sleep(
                    _wait_upstream_retry(
                        SimpleNamespace(
                            attempt_number=attempt,
                            outcome=SimpleNamespace(exception=lambda error=error: error),
                        )
                    )
                )

    @staticmethod
    def _safe_request_id(value) -> str | None:
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value)
            else None
        )

    async def ping(self, role: LLMRole) -> tuple[str, int]:
        before = self.tokens_used
        result = await self.complete_json(
            role, "只输出严格 JSON。", '返回且只返回 {"reply":"OK"}', max_tokens=128
        )
        reply = str(result.get("reply", ""))
        if reply != "OK":
            raise ValueError(f"模型连接成功但语义探针失败：{result}")
        return reply, self.tokens_used - before
