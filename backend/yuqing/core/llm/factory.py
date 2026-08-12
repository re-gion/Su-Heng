from __future__ import annotations

from collections.abc import Mapping

from openai import AsyncOpenAI

from yuqing.core.llm.roles import LLMRole, LLMRoleConfig, load_role_config


class LLMClientFactory:
    def __init__(self, environ: Mapping[str, str] | None = None):
        self.environ = environ
        self._clients: dict[str, AsyncOpenAI] = {}

    def config(self, role: LLMRole) -> LLMRoleConfig:
        return load_role_config(role, self.environ)

    def get(self, role: LLMRole) -> AsyncOpenAI:
        config = self.config(role)
        key = f"{config.base_url}|{config.api_key[:8]}"
        if key not in self._clients:
            self._clients[key] = AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout,
                max_retries=0,
            )
        return self._clients[key]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
