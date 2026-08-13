from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from yuqing.render.html import render_html
from yuqing.render.ir_migrations import CURRENT_SCHEMA_VERSION, migrate_report
from yuqing.storage.db import Database


async def migrate(args: argparse.Namespace) -> None:
    database_path = await asyncio.to_thread(Path(args.database).resolve)
    database = Database(database_path)
    await database.initialize()
    updated = 0
    try:
        rows = await database.fetch_all(
            "SELECT id,ir_json,html_path FROM report ORDER BY generated_at"
        )
        for row in rows:
            original = json.loads(row["ir_json"])
            value = migrate_report(original)
            if value == original:
                continue
            html_path = Path(row["html_path"]) if row["html_path"] else None
            if html_path is not None:
                await asyncio.to_thread(
                    html_path.write_text, render_html(value, view="full"), encoding="utf-8"
                )
            await database.execute_write(
                "UPDATE report SET ir_json=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), row["id"]),
            )
            updated += 1
        print(json.dumps({"updated": updated, "schema_version": CURRENT_SCHEMA_VERSION}))
    finally:
        await database.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="迁移并重新渲染历史报告 IR")
    parser.add_argument("--database", default="data/yuqing.db")
    asyncio.run(migrate(parser.parse_args()))


if __name__ == "__main__":
    run()
