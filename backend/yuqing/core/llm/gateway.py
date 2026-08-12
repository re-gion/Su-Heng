from __future__ import annotations

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
        config = self.factory.config(role)
        response = await self.factory.get(role).chat.completions.create(
            model=config.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=config.temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        self.calls += 1
        if response.usage:
            self.tokens_used += response.usage.total_tokens
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    async def ping(self, role: LLMRole) -> tuple[str, int]:
        before = self.tokens_used
        result = await self.complete_json(
            role, "只输出 JSON。", '返回 {"reply":"OK"}', max_tokens=16
        )
        return str(result.get("reply", "")), self.tokens_used - before
