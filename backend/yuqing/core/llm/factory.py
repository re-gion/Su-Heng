from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import SimpleNamespace

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from yuqing.core.llm.roles import LLMRole, LLMRoleConfig, load_role_config

ANTHROPIC_API_VERSION = "2023-06-01"


def _anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


class _AnthropicCompletions:
    def __init__(self, config: LLMRoleConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client

    async def create(self, **kwargs):
        messages = kwargs.get("messages", [])
        system = "\n".join(
            str(item.get("content") or "") for item in messages if item.get("role") == "system"
        )
        conversation = [
            {"role": item["role"], "content": item.get("content", "")}
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        body = {
            "model": kwargs.get("model", self.config.model),
            "max_tokens": kwargs["max_tokens"],
            "messages": conversation,
        }
        if system:
            body["system"] = system
        # Omit sampling and vendor-specific reasoning parameters. Claude model
        # generations differ on which of those fields they accept; JSON is enforced
        # by the gateway's prompt and parser for both protocols.
        request = httpx.Request("POST", _anthropic_messages_url(self.config.base_url))
        try:
            response = await self.client.post(
                request.url,
                headers={
                    "x-api-key": self.config.api_key,
                    "anthropic-version": ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                },
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise APITimeoutError(request=request) from exc
        except httpx.TransportError as exc:
            raise APIConnectionError(request=request) from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.is_error:
            error = payload.get("error")
            message = (
                str(error.get("message") or "")[:1000] if isinstance(error, dict) else ""
            ) or f"Anthropic Messages 接口返回 HTTP {response.status_code}"
            error_args = {"response": response, "body": payload}
            if response.status_code == 429:
                raise RateLimitError(message, **error_args)
            if response.status_code in {401, 403}:
                raise AuthenticationError(message, **error_args)
            if response.status_code == 400:
                raise BadRequestError(message, **error_args)
            if response.status_code >= 500:
                raise InternalServerError(message, **error_args)
            raise APIStatusError(message, **error_args)

        content = payload.get("content")
        text = "\n".join(
            block["text"]
            for block in content or []
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = (
            input_tokens + output_tokens
            if isinstance(input_tokens, int) and isinstance(output_tokens, int)
            else None
        )
        normalized_usage = SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
            completion_tokens_details=None,
        )
        finish_reason = payload.get("stop_reason")
        if finish_reason == "max_tokens":
            finish_reason = "length"
        return SimpleNamespace(
            usage=normalized_usage,
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)
            ],
            _request_id=payload.get("request_id") or payload.get("id"),
        )


class _AnthropicClient:
    def __init__(
        self,
        config: LLMRoleConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(timeout=config.timeout, transport=transport)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_AnthropicCompletions(config, self._http).create)
        )

    async def close(self) -> None:
        await self._http.aclose()


class LLMClientFactory:
    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        anthropic_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.environ = environ
        self.anthropic_transport = anthropic_transport
        self._clients: dict[str, AsyncOpenAI | _AnthropicClient] = {}

    def config(self, role: LLMRole) -> LLMRoleConfig:
        return load_role_config(role, self.environ)

    def get(self, role: LLMRole) -> AsyncOpenAI | _AnthropicClient:
        config = self.config(role)
        key = (
            f"{config.protocol}|{config.base_url}|"
            f"{hashlib.sha256(config.api_key.encode()).hexdigest()}"
        )
        if key not in self._clients:
            if config.protocol == "anthropic":
                client = _AnthropicClient(config, transport=self.anthropic_transport)
            else:
                client = AsyncOpenAI(
                    api_key=config.api_key,
                    base_url=config.base_url,
                    timeout=config.timeout,
                    max_retries=0,
                )
            self._clients[key] = client
        return self._clients[key]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
