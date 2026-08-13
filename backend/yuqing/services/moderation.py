from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from yuqing.core.llm.gateway import LLMGateway
from yuqing.services.forum import ForumMessage


class ReviewGap(BaseModel):
    agent: str
    desc: str
    priority: str = "medium"


class ReviewConflict(BaseModel):
    claim_ids: list[str] = Field(default_factory=list)
    desc: str
    resolution_hint: str | None = None


class ReviewDirective(BaseModel):
    agent: str
    instruction: str


class ModeratorReview(BaseModel):
    gaps: list[ReviewGap] = Field(default_factory=list)
    blind_spots: list[dict] = Field(default_factory=list)
    conflicts: list[ReviewConflict] = Field(default_factory=list)
    unresolved_critical: list[str] = Field(default_factory=list)
    directives: list[ReviewDirective] = Field(default_factory=list)
    release: bool = False
    reason: str
    degraded: bool = False


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
        prompt = (
            f"事件：{event_query}\n证据数：{evidence_count}；claim 数：{claim_count}\n"
            f"论坛记录：\n{digest}\n"
            "输出 JSON；gaps 每项必须是 {agent,desc,priority} 对象，"
            "directives 每项必须是 {agent,instruction} 对象，conflicts 每项必须是对象，"
            "blind_spots 每项必须是对象；同时输出 unresolved_critical/release/reason。"
            "没有矛盾不要构造；release=true 时不得存在 high gap。"
        )
        try:
            value = await self.gateway.complete_json(
                "moderator", self.system_prompt, prompt, max_tokens=4000
            )
        except Exception as first_call_error:
            try:
                value = await self.gateway.complete_json(
                    "moderator",
                    self.system_prompt,
                    prompt
                    + "\n上一次响应无法解析。请缩短文字并只输出一个完整 JSON 对象。"
                    + f"\n错误：{type(first_call_error).__name__}",
                    max_tokens=4000,
                )
            except Exception:
                return self._fallback()
        try:
            return ModeratorReview.model_validate(value)
        except ValidationError as first_error:
            try:
                corrected = await self.gateway.complete_json(
                    "moderator",
                    self.system_prompt,
                    prompt
                    + "\n上一次输出未通过 schema。请修正，数组元素不得使用字符串。"
                    + f"\n校验错误：{str(first_error)[:1200]}\n上次输出：{str(value)[:4000]}",
                    max_tokens=4000,
                )
            except Exception:
                return self._fallback()
            try:
                return ModeratorReview.model_validate(corrected)
            except ValidationError:
                return self._fallback()

    @staticmethod
    def _fallback() -> ModeratorReview:
        return ModeratorReview(
            release=True,
            degraded=True,
            reason="主持人结构化输出连续两次不合格，系统按现有证据强制放行。",
            unresolved_critical=["主持人评审未能形成合格的结构化缺口清单。"],
        )
