from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.roles import LLMRole
from yuqing.core.text_safety import repair_unicode_scalars

# 推理模型会在可见正文前消耗 max_tokens 输出思考内容；预算过小时正文为空或截断。
# 命中长度截断时按倍数放大预算重试，封顶避免无界膨胀。
LENGTH_RETRY_GROWTH = 4
LENGTH_RETRY_MAX_TOKENS = 8192

# 5xx 网关故障常伴随约 60s 等待与 HTML 错误页；三次浅重试撑不过故障窗口，
# 且错误页原文一旦进入用户可见消息就违反"不暴露上游响应全文"的约定。
# 退避序列 2/4/8/16/32 秒累计约 62 秒，覆盖注释所述故障窗口。
UPSTREAM_RETRY_ATTEMPTS = 6
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


def is_upstream_failure(exc: BaseException) -> bool:
    """上游服务/网关故障（换时间重试可能成功），区别于材料本身的语义问题。"""
    return isinstance(exc, UPSTREAM_RETRY_EXCEPTIONS)


def sanitize_upstream_message(exc: BaseException) -> str:
    """带 HTTP 状态码的上游错误收敛为一句话（剥掉网关错误页等响应体原文）；其余异常原样保留。"""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"上游服务返回 {status}（{type(exc).__name__}）"
    return f"{type(exc).__name__}: {exc}"


class LLMGateway:
    def __init__(self, factory: LLMClientFactory):
        self.factory = factory
        self.tokens_used = 0
        self.calls = 0
        self.token_limit: int | None = None
        self._tokens_reserved = 0
        self._budget_lock = asyncio.Lock()

    @retry(
        stop=stop_after_attempt(UPSTREAM_RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=2, max=UPSTREAM_RETRY_MAX_WAIT),
        retry=retry_if_exception_type(UPSTREAM_RETRY_EXCEPTIONS),
        reraise=True,
    )
    async def complete_json(
        self, role: LLMRole, system: str, user: str, *, max_tokens: int = 2000
    ) -> dict[str, Any]:
        reservation = len(system) + len(user) + max_tokens
        async with self._budget_lock:
            if (
                self.token_limit is not None
                and self.tokens_used + self._tokens_reserved + reservation > self.token_limit
            ):
                raise RuntimeError("LLM token budget exhausted")
            self._tokens_reserved += reservation
        config = self.factory.config(role)
        try:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]

            async def create(budget: int):
                return await self.factory.get(role).chat.completions.create(
                    model=config.model,
                    messages=messages,
                    temperature=config.temperature,
                    max_tokens=budget,
                    response_format={"type": "json_object"},
                )

            response = await create(max_tokens)
            # 推理模型可能把预算耗在思考上：命中长度截断且预算可放大时，换更大预算重试。
            while (
                getattr(response.choices[0], "finish_reason", None) if response.choices else None
            ) == "length" and max_tokens < LENGTH_RETRY_MAX_TOKENS:
                max_tokens = min(max_tokens * LENGTH_RETRY_GROWTH, LENGTH_RETRY_MAX_TOKENS)
                response = await create(max_tokens)
        finally:
            async with self._budget_lock:
                self._tokens_reserved -= reservation
        self.calls += 1
        if response.usage:
            self.tokens_used += response.usage.total_tokens
        content = response.choices[0].message.content or "{}"
        return repair_unicode_scalars(json.loads(content))

    async def ping(self, role: LLMRole) -> tuple[str, int]:
        before = self.tokens_used
        result = await self.complete_json(
            role, "只输出严格 JSON。", '返回且只返回 {"reply":"OK"}', max_tokens=128
        )
        reply = str(result.get("reply", ""))
        if reply != "OK":
            raise ValueError(f"模型连接成功但语义探针失败：{result}")
        return reply, self.tokens_used - before
