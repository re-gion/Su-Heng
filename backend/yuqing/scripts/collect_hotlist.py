from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from yuqing.services.historical_data import HistoricalDataService
from yuqing.services.hotlist import DailyHotCollector
from yuqing.storage.db import Database


async def collect(args: argparse.Namespace) -> None:
    database_path = await asyncio.to_thread(Path(args.database).resolve)
    database = Database(database_path)
    await database.initialize()
    collector = DailyHotCollector(
        HistoricalDataService(database),
        [item.strip() for item in args.base_urls.split(",") if item.strip()],
    )
    try:
        result = await collector.collect(
            [item.strip() for item in args.platforms.split(",") if item.strip()]
        )
        print(
            json.dumps(
                {"inserted": result.inserted, "platforms": result.platforms}, ensure_ascii=False
            )
        )
    finally:
        await collector.aclose()
        await database.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="采集公开热榜快照")
    parser.add_argument("--database", default="data/yuqing.db")
    parser.add_argument("--base-urls", required=True, help="DailyHotApi 地址，逗号分隔")
    parser.add_argument("--platforms", default="weibo,zhihu,douyin,toutiao")
    asyncio.run(collect(parser.parse_args()))


if __name__ == "__main__":
    run()
