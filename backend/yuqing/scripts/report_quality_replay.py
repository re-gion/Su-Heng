"""Rebuild selected real tasks in a private copy, never update the source database."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from yuqing.app.main import _build_default_orchestrator
from yuqing.core.events import EventBus
from yuqing.services.forum import ForumBoard
from yuqing.storage.db import Database


def copy_database(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    target = output / "yuqing.db"
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as copied:
            original.backup(copied)
    return target


async def replay(
    source: Path | None,
    output: Path,
    task_ids: list[str],
    supplement: bool,
    resume: bool = False,
    refresh_comments: bool = False,
    refresh_history: bool = False,
):
    if resume:
        if not output.name.startswith("report-quality-replay-"):
            raise ValueError("resume is restricted to a report-quality-replay directory")
        target = output / "yuqing.db"
        same_source = source is not None and await asyncio.to_thread(
            lambda: target.resolve() == source.resolve()
        )
        if not target.is_file() or same_source:
            raise ValueError("an existing, separate replay database is required")
    else:
        if source is None:
            raise ValueError("--source is required for a new replay")
        target = await asyncio.to_thread(copy_database, source, output)
    database = Database(target)
    await database.initialize()
    result_file = output / "results.json"
    results = (
        json.loads(result_file.read_text(encoding="utf-8"))
        if resume and result_file.exists()
        else []
    )
    try:
        for task_id in task_ids:
            if any(item.get("task") == task_id for item in results):
                continue
            orchestrator = _build_default_orchestrator(output)(database, EventBus(database))
            try:
                task = await database.get_task(task_id)
                if task is None:
                    raise ValueError("task not found")
                await database.set_task_status(task_id, "running", "reporting")
                orchestrator._budget_depth = task.depth
                orchestrator.usage.token_limit = orchestrator.budget_for(task.depth).token_limit
                orchestrator.usage.tokens_used = task.tokens_used
                budget_row = await database.fetch_one(
                    "SELECT payload FROM event_log WHERE task_id=? AND event_type='budget.update' ORDER BY seq DESC LIMIT 1",
                    (task_id,),
                )
                if budget_row:
                    prior = json.loads(budget_row["payload"]).get("data", {})
                    orchestrator._search_calls = int(prior.get("search_calls", 0))
                    orchestrator._fetch_calls = int(prior.get("fetch_calls", 0))
                if refresh_comments and task.comment_mode != "off":
                    await database.execute_write(
                        "DELETE FROM task_state WHERE task_id=? AND step_key='comments:analysis'",
                        (task_id,),
                    )
                if refresh_history:
                    await database.execute_write(
                        "DELETE FROM task_state WHERE task_id=? AND step_key='report:recovery:history_insight'",
                        (task_id,),
                    )
                board = await ForumBoard.restore(database, task_id)
                print(json.dumps({"task": task_id, "stage": "comments"}), flush=True)
                if task.comment_mode != "off":
                    await orchestrator._run_comment_insight(task_id, task.event_query, board)
                if supplement:
                    print(json.dumps({"task": task_id, "stage": "supplement"}), flush=True)
                    await orchestrator._recover_report_gaps(
                        task_id, task.event_query + f"\n观察截止日期：{task.time_range_to}", board
                    )
                print(json.dumps({"task": task_id, "stage": "report"}), flush=True)
                report_id, report, path = await orchestrator.reports.build(
                    task_id,
                    forum=board.history(),
                    orchestration_limitations=orchestrator._limitations,
                )
                ir_path = output / f"{task_id}.json"
                await asyncio.to_thread(
                    ir_path.write_text,
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                result = {
                    "task": task_id,
                    "report_id": report_id,
                    "html": path,
                    "quality": report["quality"],
                    "tokens_used": orchestrator.usage.tokens_used,
                    "comment_coverage": next(
                        (
                            b.get("coverage")
                            for b in report["blocks"]
                            if b["type"] == "comment_insight"
                        ),
                        None,
                    ),
                }
                results.append(result)
                await asyncio.to_thread(
                    (output / "results.json").write_text,
                    json.dumps(results, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                await orchestrator.aclose()
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--supplement", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--refresh-comments", action="store_true")
    parser.add_argument("--refresh-history", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        replay(
            args.source,
            args.output,
            args.tasks,
            args.supplement,
            resume=args.resume,
            refresh_comments=args.refresh_comments,
            refresh_history=args.refresh_history,
        )
    )


if __name__ == "__main__":
    main()
