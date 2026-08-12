from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from yuqing.storage.db import Database

EventType = Literal[
    "task.status",
    "agent.status",
    "agent.token",
    "search.result",
    "evidence.added",
    "claim.added",
    "forum.message",
    "host.review",
    "loop.round",
    "verify.progress",
    "report.section",
    "report.done",
    "budget.update",
    "warning",
    "error",
]


class EventEnvelope(BaseModel):
    event: EventType
    task_id: str
    seq: int
    ts: str
    data: dict[str, Any]


class EventBus:
    """事件先持久化，再向热路径订阅者广播。"""

    def __init__(self, database: Database, *, subscriber_buffer_size: int = 500):
        self.database = database
        self.subscriber_buffer_size = subscriber_buffer_size
        self._subscribers: dict[str, set[asyncio.Queue[EventEnvelope]]] = defaultdict(set)

    async def emit(
        self, task_id: str, event_type: EventType, data: dict[str, Any]
    ) -> EventEnvelope:
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")

        def operation(connection):
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM event_log WHERE task_id=?", (task_id,)
            ).fetchone()
            sequence = int(row[0])
            event = EventEnvelope(
                event=event_type, task_id=task_id, seq=sequence, ts=stamp, data=data
            )
            connection.execute(
                "INSERT INTO event_log(task_id,seq,event_type,ts,payload) VALUES(?,?,?,?,?)",
                (task_id, sequence, event_type, stamp, event.model_dump_json()),
            )
            return event

        envelope = await self.database.write(operation)
        for queue in tuple(self._subscribers.get(task_id, ())):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                queue.get_nowait()
                queue.put_nowait(envelope)
        return envelope

    def subscribe(self, task_id: str) -> asyncio.Queue[EventEnvelope]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=self.subscriber_buffer_size)
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[EventEnvelope]) -> None:
        subscribers = self._subscribers.get(task_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(task_id, None)

    async def high_water(self, task_id: str) -> int:
        row = await self.database.fetch_one(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM event_log WHERE task_id=?", (task_id,)
        )
        return int(row["seq"]) if row else 0

    async def history(
        self, task_id: str, *, after_seq: int = 0, through_seq: int | None = None
    ) -> list[EventEnvelope]:
        if through_seq is None:
            rows = await self.database.fetch_all(
                "SELECT payload FROM event_log WHERE task_id=? AND seq>? ORDER BY seq",
                (task_id, after_seq),
            )
        else:
            rows = await self.database.fetch_all(
                "SELECT payload FROM event_log WHERE task_id=? AND seq>? AND seq<=? ORDER BY seq",
                (task_id, after_seq, through_seq),
            )
        return [EventEnvelope.model_validate(json.loads(row["payload"])) for row in rows]
