from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from yuqing.services.historical_data import (
    DatasetAssetInput,
    HistoricalDataService,
    HistoricalEventInput,
)
from yuqing.storage.db import Database


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [item.strip() for item in re_split(str(value)) if item.strip()]


def re_split(value: str) -> list[str]:
    import re

    return re.split(r"[,，;；|]", value)


def _rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("items") or payload.get("events") or []
        if not isinstance(payload, list):
            raise ValueError("JSON 顶层必须是数组，或含 data/items/events 数组")
        return [item for item in payload if isinstance(item, dict)]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    raise ValueError("仅支持 .json/.jsonl/.csv")


def load_event_records(path: Path) -> tuple[list[HistoricalEventInput], int]:
    events: list[HistoricalEventInput] = []
    skipped = 0
    for row in _rows(path):
        event_name = _first(row, "event_name", "title", "topic", "name", "关键词")
        if not event_name:
            skipped += 1
            continue
        summary = _first(row, "summary", "description", "content", "desc", "经过") or event_name
        source_url = _first(row, "source_url", "url", "link")
        if not source_url:
            skipped += 1
            continue
        try:
            events.append(
                HistoricalEventInput(
                    event_name=str(event_name),
                    event_time_start=_first(
                        row, "event_time_start", "date", "time", "timestamp", "日期"
                    ),
                    event_time_end=_first(row, "event_time_end", "end_date", "结束时间"),
                    summary=str(summary),
                    outcome=_first(row, "outcome", "result", "结局"),
                    nature=_first(row, "nature", "category", "事件性质"),
                    outbreak_path=_first(row, "outbreak_path", "传播路径", "爆发路径"),
                    response=_first(row, "response", "机构应对", "企业应对"),
                    regulatory_involvement=_first(row, "regulatory_involvement", "监管介入"),
                    source_url=source_url,
                    source_title=_first(row, "source_title", "来源标题"),
                    source_name=_first(row, "source_name", "source", "来源"),
                    source_published_at=_first(row, "source_published_at", "published_at"),
                    keywords=_list(_first(row, "keywords", "tags", "关键词列表")),
                    aliases=_list(_first(row, "aliases", "别名")),
                )
            )
        except (TypeError, ValueError):
            skipped += 1
    return events, skipped


async def import_file(args: argparse.Namespace) -> None:
    path = await asyncio.to_thread(Path(args.file).resolve)
    raw = await asyncio.to_thread(path.read_bytes)
    events, invalid = await asyncio.to_thread(load_event_records, path)
    database_path = await asyncio.to_thread(Path(args.database).resolve)
    database = Database(database_path)
    await database.initialize()
    try:
        result = await HistoricalDataService(database).import_events(
            DatasetAssetInput(
                slug=args.slug,
                name=args.name,
                source_url=args.source_url,
                license_label=args.license_label,
                upstream_rights_note=args.rights_note,
                redistribution=args.redistribution,
                content_sha256=hashlib.sha256(raw).hexdigest(),
                metadata={"source_file": path.name, "invalid_rows": invalid},
            ),
            events,
        )
        print(
            json.dumps(
                {
                    "asset": result.asset.slug,
                    "imported": result.imported,
                    "duplicates": result.skipped,
                    "invalid": invalid,
                    "total": result.asset.record_count,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await database.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="导入字段白名单化的历史事件数据集")
    parser.add_argument("--file", required=True, help=".json/.jsonl/.csv 文件")
    parser.add_argument("--database", default="data/yuqing.db")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--license-label", required=True)
    parser.add_argument("--rights-note", required=True)
    parser.add_argument("--redistribution", choices=("allowed", "restricted"), required=True)
    asyncio.run(import_file(parser.parse_args()))


if __name__ == "__main__":
    run()
