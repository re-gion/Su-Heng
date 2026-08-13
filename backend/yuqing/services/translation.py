from __future__ import annotations

from typing import Protocol

from yuqing.core.llm.gateway import LLMGateway


class Translator(Protocol):
    async def translate_to_chinese(self, text: str, source_lang: str) -> str: ...


class OpenAITranslator:
    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def translate_to_chinese(self, text: str, source_lang: str) -> str:
        result = await self.gateway.complete_json(
            "utility",
            "你是证据翻译器。忠实翻译，不补充解释，不改变数字、专名和不确定性。只输出 JSON。",
            f'源语言：{source_lang}\n原文：{text[:3000]}\n输出 {{"translation_zh":"..."}}。',
            max_tokens=1200,
        )
        return str(result.get("translation_zh") or "").strip()
