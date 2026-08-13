from __future__ import annotations

from typing import Any

from yuqing.core.llm.gateway import LLMGateway


class OpenAIReportAgent:
    def __init__(self, gateway: LLMGateway, system_prompt: str):
        self.gateway = gateway
        self.system_prompt = system_prompt

    async def enrich(self, context: dict[str, Any]) -> dict[str, Any]:
        return await self.gateway.complete_json(
            "reporter",
            self.system_prompt,
            "以下是数据库摘要与主持人决议。只输出 JSON："
            '{"organization_note":"一句组织建议","section_warnings":["需降级的章节"]}。\n'
            + str(context)[:20000],
            max_tokens=800,
        )
