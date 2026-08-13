from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.roles import LLMRole


class LLMGateway:
    def __init__(self, factory: LLMClientFactory):
        self.factory = factory
        self.tokens_used = 0
        self.calls = 0
        self.token_limit: int | None = None
        self._tokens_reserved = 0
        self._budget_lock = asyncio.Lock()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(
            (
                TimeoutError,
                ConnectionError,
                json.JSONDecodeError,
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                InternalServerError,
            )
        ),
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
            response = await self.factory.get(role).chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=config.temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        finally:
            async with self._budget_lock:
                self._tokens_reserved -= reservation
        self.calls += 1
        if response.usage:
            self.tokens_used += response.usage.total_tokens
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    async def ping(self, role: LLMRole) -> tuple[str, int]:
        before = self.tokens_used
        result = await self.complete_json(
            role, "只输出严格 JSON。", '返回且只返回 {"reply":"OK"}', max_tokens=128
        )
        reply = str(result.get("reply", ""))
        if reply != "OK":
            raise ValueError(f"模型连接成功但语义探针失败：{result}")
        return reply, self.tokens_used - before
