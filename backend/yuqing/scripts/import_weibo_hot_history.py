from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx

from yuqing.services.historical_data import (
    DatasetAssetInput,
    HistoricalDataService,
    HotSnapshotInput,
)
from yuqing.services.hotlist import parse_heat_value
from yuqing.storage.db import Database

SOURCE_URL = "https://github.com/lxw15337674/weibo-trending-hot-history"
RAW_TEMPLATE = (
    "https://raw.githubusercontent.com/lxw15337674/weibo-trending-hot-history/"
    "master/archives/{day}.md"
)


def parse_weibo_archive(markdown: str, day: str) -> list[HotSnapshotInput]:
    updated = re.search(
        r"最后更新时间：\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*([AP]M)", markdown
    )
    if updated:
        parsed = datetime.strptime(
            f"{updated.group(1)} {updated.group(2)} {updated.group(3)}", "%Y-%m-%d %I:%M %p"
        ).replace(tzinfo=timezone(timedelta(hours=8)))
    else:
        parsed = datetime.combine(
            date.fromisoformat(day), time(23, 59), timezone(timedelta(hours=8))
        )
    captured_at = parsed.isoformat(timespec="seconds")
    pattern = re.compile(
        r"^\s*(\d+)\.\s+\[([^]]+)]\((https?://[^)]+)\)\s+`([^`]*)`\s+-\s+([^\s]+)\s*$",
        re.MULTILINE,
    )
    return [
        HotSnapshotInput(
            platform="weibo",
            captured_at=captured_at,
            rank=int(match.group(1)),
            title=match.group(2).strip(),
            heat_value=parse_heat_value(match.group(5)),
            url=match.group(3),
            raw={"category": match.group(4), "archive_date": day},
        )
        for match in pattern.finditer(markdown)
    ]


async def import_history(args: argparse.Namespace) -> None:
    if not args.acknowledge_upstream_rights:
        raise SystemExit(
            "必须显式传 --acknowledge-upstream-rights：仓库 MIT 标签不自动授权上游微博榜单数据。"
        )
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    if end < start or (end - start).days > 366:
        raise SystemExit("日期范围必须正序且不超过 367 天")
    database_path = await asyncio.to_thread(Path(args.database).resolve)
    database = Database(database_path)
    await database.initialize()
    history = HistoricalDataService(database)
    contents: list[bytes] = []
    points: list[HotSnapshotInput] = []
    missing: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            current = start
            while current <= end:
                day = current.isoformat()
                response = await client.get(RAW_TEMPLATE.format(day=day))
                if response.status_code == 404:
                    missing.append(day)
                else:
                    response.raise_for_status()
                    contents.append(response.content)
                    points.extend(parse_weibo_archive(response.text, day))
                current += timedelta(days=1)
        digest = hashlib.sha256(b"\n".join(contents)).hexdigest() if contents else None
        asset = await history.register_asset(
            DatasetAssetInput(
                slug="weibo-trending-hot-history",
                name="微博热搜历史日归档",
                source_url=SOURCE_URL,
                license_label="repository-MIT",
                upstream_rights_note="MIT 仅覆盖仓库作者产出，不自动构成上游榜单数据再许可。",
                redistribution="restricted",
                content_sha256=digest,
            ),
            record_count=0,
            metadata={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "missing_days": missing,
                "fields": ["title", "rank", "heat_value", "captured_at", "url", "category"],
            },
        )
        bound_points = [item.model_copy(update={"asset_id": asset.id}) for item in points]
        inserted = await history.record_hot_snapshots(bound_points)
        total = await database.fetch_one(
            "SELECT COUNT(*) AS total FROM hot_snapshot WHERE asset_id=?", (asset.id,)
        )
        await history.register_asset(
            DatasetAssetInput(
                slug="weibo-trending-hot-history",
                name="微博热搜历史日归档",
                source_url=SOURCE_URL,
                license_label="repository-MIT",
                upstream_rights_note="MIT 仅覆盖仓库作者产出，不自动构成上游榜单数据再许可。",
                redistribution="restricted",
                content_sha256=digest,
            ),
            record_count=int(total["total"] if total else 0),
            metadata={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "missing_days": missing,
                "fields": ["title", "rank", "heat_value", "captured_at", "url", "category"],
            },
        )
        print(
            json.dumps(
                {
                    "downloaded": len(contents),
                    "parsed": len(points),
                    "inserted": inserted,
                    "missing": missing,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await database.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="一键导入微博热搜历史公开日归档")
    parser.add_argument("--database", default="data/yuqing.db")
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--acknowledge-upstream-rights", action="store_true")
    asyncio.run(import_history(parser.parse_args()))


if __name__ == "__main__":
    run()
