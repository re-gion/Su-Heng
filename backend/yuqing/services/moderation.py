from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yuqing.core.llm.gateway import LLMGateway
from yuqing.services.forum import ForumMessage


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewGap(ReviewModel):
    agent: str
    desc: str
    priority: Literal["low", "medium", "high"] = "medium"


class ReviewBlindSpot(ReviewModel):
    desc: str
    suggested_query: str | None = None


class ReviewConflict(ReviewModel):
    claim_ids: list[str] = Field(default_factory=list)
    desc: str
    resolution_hint: str | None = None


class ReviewDirective(ReviewModel):
    agent: str
    instruction: str


class ModeratorReview(ReviewModel):
    gaps: list[ReviewGap] = Field(default_factory=list)
    blind_spots: list[ReviewBlindSpot] = Field(default_factory=list)
    conflicts: list[ReviewConflict] = Field(default_factory=list)
    unresolved_critical: list[str] = Field(default_factory=list)
    directives: list[ReviewDirective] = Field(default_factory=list)
    release: bool = False
    reason: str
    degraded: bool = False
    diagnostics: list[str] = Field(default_factory=list, exclude=True)


class Moderator(Protocol):
    async def review(
        self,
        event_query: str,
        forum: Sequence[ForumMessage],
        evidence_count: int,
        claim_count: int,
    ) -> ModeratorReview: ...


class OpenAIModerator:
    def __init__(self, gateway: LLMGateway, system_prompt: str):
        self.gateway = gateway
        self.system_prompt = system_prompt

    async def review(
        self,
        event_query: str,
        forum: Sequence[ForumMessage],
        evidence_count: int,
        claim_count: int,
    ) -> ModeratorReview:
        digest = "\n".join(f"[{item.agent}/{item.type}] {item.content}" for item in forum[-30:])[
            :20000
        ]
        schema = self._output_schema()
        prompt = (
            f"事件：{event_query}\n证据数：{evidence_count}；claim 数：{claim_count}\n"
            f"论坛记录：\n{digest}\n"
            "只输出一个符合下列 JSON Schema 的对象，不得添加 Schema 之外的字段：\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
            "没有矛盾不要构造；release=true 时不得存在 high gap 或 unresolved_critical。"
        )
        diagnostics: list[str] = []
        correction = ""
        previous: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                value = await self.gateway.complete_json(
                    "moderator",
                    self.system_prompt,
                    prompt + correction,
                    max_tokens=4000,
                )
            except Exception as exc:
                diagnostics.append(f"response:{type(exc).__name__}")
            else:
                previous = value
                try:
                    return ModeratorReview.model_validate(value)
                except ValidationError as exc:
                    diagnostics.extend(self._validation_diagnostics(exc))
            if attempt == 0:
                prior = (
                    json.dumps(previous, ensure_ascii=False)[:4000]
                    if previous is not None
                    else "上一次响应无法解析为完整 JSON 对象"
                )
                correction = (
                    "\n上一次输出未通过校验。请依据 JSON Schema 修正后重新输出。"
                    f"\n校验路径：{'; '.join(diagnostics[-12:])}\n上次输出：{prior}"
                )
        return self._fallback(diagnostics)

    @staticmethod
    def _output_schema() -> dict[str, Any]:
        schema = ModeratorReview.model_json_schema()
        properties = schema.get("properties", {})
        for field in ("degraded", "diagnostics"):
            properties.pop(field, None)
        schema["required"] = [
            field
            for field in schema.get("required", [])
            if field not in {"degraded", "diagnostics"}
        ]
        return schema

    @staticmethod
    def _validation_diagnostics(error: ValidationError) -> list[str]:
        diagnostics = []
        for item in error.errors(include_input=False, include_url=False):
            path = ".".join(str(part) for part in item.get("loc", ())) or "root"
            diagnostics.append(f"schema:{path}:{item.get('type', 'invalid')}")
        return diagnostics

    @staticmethod
    def _fallback(diagnostics: list[str]) -> ModeratorReview:
        return ModeratorReview(
            gaps=[
                ReviewGap(
                    agent="fact_investigator",
                    desc="主持人评审不可用，关键证据缺口需在下一轮重新核对。",
                    priority="high",
                )
            ],
            directives=[
                ReviewDirective(
                    agent="fact_investigator",
                    instruction="重新核对关键陈述与已绑定证据，优先补取一手材料。",
                ),
                ReviewDirective(
                    agent="media_propagation",
                    instruction="复核传播材料的同源转载与缺失原文，补充独立信源。",
                ),
            ],
            release=False,
            degraded=True,
            reason="主持人结构化输出两次未通过校验，评审已降级并进入下一轮调查。",
            unresolved_critical=["主持人评审未能形成合格的结构化缺口清单。"],
            diagnostics=diagnostics,
        )
