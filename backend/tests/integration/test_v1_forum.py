import pytest

from yuqing.services.forum import ForumBoard, ForumMessageCreate
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate


@pytest.mark.asyncio
async def test_forum_post_is_persisted_broadcast_and_restored_after_restart(runtime_dir):
    database = Database(runtime_dir / "forum.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="论坛测试"))
    board = await ForumBoard.restore(database, task.id)
    subscription = board.subscribe()

    posted = await board.post(
        ForumMessageCreate(
            task_id=task.id,
            round=1,
            agent="fact_investigator",
            type="finding",
            content="监管部门已发布公开通报。",
            refs=["E001", "C001"],
        )
    )

    assert posted.id == 1
    assert (await subscription.get()).content == "监管部门已发布公开通报。"
    await database.close()

    restarted = Database(runtime_dir / "forum.db")
    await restarted.initialize()
    restored = await ForumBoard.restore(restarted, task.id)
    assert [message.model_dump(mode="json") for message in restored.history()] == [
        posted.model_dump(mode="json")
    ]
    await restarted.close()


@pytest.mark.asyncio
async def test_forum_digest_only_contains_relevant_context(runtime_dir):
    database = Database(runtime_dir / "digest.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="摘要测试"))
    board = await ForumBoard.restore(database, task.id)
    for message in (
        ForumMessageCreate(
            task_id=task.id,
            round=1,
            agent="media_propagation",
            type="summary",
            content="媒体转载集中在事件发生后的两天。",
        ),
        ForumMessageCreate(
            task_id=task.id,
            round=1,
            agent="moderator",
            type="directive",
            content="补查监管部门回应。",
            payload={"agent": "fact_investigator"},
        ),
        ForumMessageCreate(
            task_id=task.id,
            round=1,
            agent="moderator",
            type="directive",
            content="补查历史案例。",
            payload={"agent": "history_insight"},
        ),
    ):
        await board.post(message)

    digest = board.digest_for("fact_investigator", 1)
    assert "媒体转载集中" in digest
    assert "补查监管部门回应" in digest
    assert "补查历史案例" not in digest
    await database.close()
