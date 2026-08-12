import asyncio

import pytest

from yuqing.core.events import EventBus
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate


@pytest.mark.asyncio
async def test_subscribe_before_replay_has_no_gap_or_duplicate(runtime_dir):
    database = Database(runtime_dir / "yuqing.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="SSE 重连测试", depth="quick"))
    bus = EventBus(database)
    for index in range(3):
        await bus.emit(task.id, "agent.status", {"index": index})

    subscription = bus.subscribe(task.id)
    high_water = await bus.high_water(task.id)
    pending = asyncio.create_task(bus.emit(task.id, "evidence.added", {"index": 3}))
    history = await bus.history(task.id, after_seq=1, through_seq=high_water)
    await pending
    live = await asyncio.wait_for(subscription.get(), timeout=1)
    bus.unsubscribe(task.id, subscription)

    received = [event.seq for event in history] + [live.seq]
    assert received == [2, 3, 4]
    assert len(received) == len(set(received))
    assert await bus.high_water(task.id) == 4
    await database.close()


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_break_persisted_event_delivery(runtime_dir):
    database = Database(runtime_dir / "slow-subscriber.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="慢订阅者", depth="quick"))
    bus = EventBus(database, subscriber_buffer_size=2)
    subscription = bus.subscribe(task.id)

    for index in range(3):
        await bus.emit(task.id, "agent.status", {"index": index})

    buffered = [subscription.get_nowait().seq, subscription.get_nowait().seq]
    history = await bus.history(task.id)
    assert buffered == [2, 3]
    assert [event.seq for event in history] == [1, 2, 3]
    await database.close()
