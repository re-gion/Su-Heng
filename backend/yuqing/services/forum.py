from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from yuqing.storage.db import Database, now_iso

ForumMessageType = Literal[
    "finding", "summary", "question", "review", "directive", "conflict", "system"
]


class ForumMessageCreate(BaseModel):
    task_id: str
    round: int = Field(ge=1)
    agent: str
    type: ForumMessageType
    content: str = Field(min_length=1)
    refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] | None = None


class ForumMessage(ForumMessageCreate):
    id: int
    created_at: datetime


class ForumBoard:
    """任务内 append-only 论坛；SQLite 是恢复时的权威来源。"""

    def __init__(self, database: Database, task_id: str, messages: list[ForumMessage]):
        self.database = database
        self.task_id = task_id
        self._messages = messages
        self._subscribers: set[asyncio.Queue[ForumMessage]] = set()
        self._lock = asyncio.Lock()

    @classmethod
    async def restore(cls, database: Database, task_id: str) -> ForumBoard:
        rows = await database.list_forum_messages(task_id)
        messages = [ForumMessage.model_validate(dict(row)) for row in rows]
        return cls(database, task_id, messages)

    async def post(self, value: ForumMessageCreate) -> ForumMessage:
        if value.task_id != self.task_id:
            raise ValueError("论坛消息 task_id 与 ForumBoard 不一致")
        async with self._lock:
            row = await self.database.add_forum_message(
                task_id=value.task_id,
                round_number=value.round,
                agent=value.agent,
                message_type=value.type,
                content=value.content,
                refs=value.refs,
                payload=value.payload,
                created_at=now_iso(),
            )
            message = ForumMessage.model_validate(dict(row))
            self._messages.append(message)
            for queue in tuple(self._subscribers):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(message)
            return message

    def subscribe(self, *, maxsize: int = 256) -> asyncio.Queue[ForumMessage]:
        queue: asyncio.Queue[ForumMessage] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ForumMessage]) -> None:
        self._subscribers.discard(queue)

    def history(self, *, since_id: int = 0, round_number: int | None = None) -> list[ForumMessage]:
        return [
            message
            for message in self._messages
            if message.id > since_id and (round_number is None or message.round == round_number)
        ]

    def digest_for(self, agent: str, round_number: int) -> str:
        relevant = []
        for message in self.history(round_number=round_number):
            if message.type == "summary" and message.agent != agent:
                relevant.append(message)
            elif message.type in {"conflict", "question"}:
                relevant.append(message)
            elif message.type == "directive" and (message.payload or {}).get("agent") == agent:
                relevant.append(message)
        return "\n".join(f"[{item.agent}/{item.type}] {item.content}" for item in relevant)
