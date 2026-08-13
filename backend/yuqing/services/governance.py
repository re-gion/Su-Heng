from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from yuqing.storage.db import Database


class ReportRetentionService:
    def __init__(self, database: Database, data_dir: Path, ttl_hours: int):
        self.database = database
        self.data_dir = Path(data_dir).resolve()
        self.ttl = timedelta(hours=max(1, ttl_hours))

    async def cleanup_expired(self) -> dict[str, int]:
        cutoff = (datetime.now().astimezone() - self.ttl).isoformat(timespec="seconds")
        rows = await self.database.fetch_all(
            """SELECT DISTINCT t.id FROM task t JOIN report r ON r.task_id=t.id
               WHERE r.generated_at<? AND t.status IN ('done','failed')""",
            (cutoff,),
        )
        deleted_tasks = 0
        deleted_files = 0
        for row in rows:
            result = await self.database.delete_task(row["id"])
            deleted_tasks += 1
            for raw_path in result.get("files", []):
                deleted_files += int(await asyncio.to_thread(self._remove_file, raw_path))
        return {"tasks": deleted_tasks, "files": deleted_files}

    def _remove_file(self, raw_path: str) -> bool:
        path = Path(raw_path).resolve()
        allowed = [self.data_dir / "snapshots", self.data_dir / "reports"]
        if not any(path == root or root in path.parents for root in allowed) or not path.is_file():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True
