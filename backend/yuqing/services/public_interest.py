from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway
from yuqing.services.configuration import ConfigService
from yuqing.storage.db import Database


class PublicInterestDecision(BaseModel):
    allowed: bool
    reason: str
    category: str


PolicyChecker = Callable[[Database, str, str | None], Awaitable[PublicInterestDecision]]


async def assess_public_interest(
    database: Database, event_query: str, user_note: str | None
) -> PublicInterestDecision:
    environ = await ConfigService(database).resolved_environ()
    factory = LLMClientFactory(environ)
    gateway = LLMGateway(factory)
    try:
        value = await gateway.complete_json(
            "utility",
            "你是任务公共性门禁。只判断是否可开展公开材料舆情调查，不调查事实。",
            "主题："
            + event_query
            + (f"\n补充：{user_note}" if user_note else "")
            + "\n只输出 JSON：allowed(boolean), reason(string), category(string)。"
            "公众人物、企业机构、公共政策、公共安全可放行；"
            "指向可识别普通个人或未成年人的人肉、私人纠纷、校园八卦式指控必须拒绝。",
            max_tokens=300,
        )
        return PublicInterestDecision.model_validate(value)
    finally:
        await factory.aclose()
