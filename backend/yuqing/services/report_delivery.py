from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Protocol

from yuqing.render.html import render_html
from yuqing.render.ir_migrations import migrate_report
from yuqing.storage.db import Database, now_iso
from yuqing.storage.snapshots import SnapshotStore


class PdfExporter(Protocol):
    async def export(self, html: str, target: Path) -> Path: ...


class ChromiumPdfExporter:
    """使用已安装 Chromium/Edge 打印，与 HTML 同源保留中文、图表和链接。"""

    def __init__(self, browser_path: str | None = None):
        self.browser_path = browser_path or os.getenv("YUQING_PDF_BROWSER")

    def _resolve_browser(self) -> str:
        candidates = [
            self.browser_path,
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("msedge"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(candidate)
        raise RuntimeError("未找到 Chromium/Edge；请设置 YUQING_PDF_BROWSER")

    async def _export_with_playwright(self, browser: str, html: str, target: Path) -> None:
        from playwright.async_api import async_playwright

        async with asyncio.timeout(90), async_playwright() as playwright:
            instance = await playwright.chromium.launch(
                executable_path=browser,
                headless=True,
            )
            try:
                page = await instance.new_page()
                await page.set_content(html, wait_until="load")
                await page.emulate_media(media="print")
                await page.pdf(
                    path=str(target),
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                await instance.close()

    def _export_in_worker(self, browser: str, html: str, target: Path) -> None:
        if sys.platform == "win32" and hasattr(asyncio, "ProactorEventLoop"):
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._export_with_playwright(browser, html, target))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    async def export(self, html: str, target: Path) -> Path:
        browser = await asyncio.to_thread(self._resolve_browser)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._export_in_worker, browser, html, target)
            if not await asyncio.to_thread(target.is_file):
                raise RuntimeError("浏览器未生成 PDF 文件")
            return target
        except TimeoutError as error:
            raise RuntimeError("浏览器 PDF 导出超时") from error
        except ImportError as error:
            raise RuntimeError("Playwright 未安装，无法导出 PDF") from error
        except NotImplementedError as error:
            raise RuntimeError("当前事件循环不支持浏览器子进程，无法导出 PDF") from error
        except RuntimeError:
            raise
        except Exception as error:
            detail = str(error).strip() or type(error).__name__
            raise RuntimeError(f"浏览器 PDF 导出失败：{detail[-500:]}") from error


class EvidencePackageBuilder:
    def __init__(self, database: Database, snapshots: SnapshotStore):
        self.database = database
        self.snapshots = snapshots

    async def build(self, report_id: str) -> bytes:
        row = await self.database.fetch_one(
            "SELECT id,task_id,ir_json,generated_at FROM report WHERE id=?", (report_id,)
        )
        if row is None:
            raise ValueError("report not found")
        report = migrate_report(json.loads(row["ir_json"]))
        html = render_html(report, view="full")
        manifest = {
            "format": "yuqing-evidence-package/1",
            "report_id": report_id,
            "task_id": row["task_id"],
            "generated_at": row["generated_at"],
            "packaged_at": now_iso(),
            "ir_schema_version": report.get("schema_version"),
            "evidence": [],
        }
        files: list[tuple[str, str]] = []
        appendix = next(
            (
                block
                for block in report.get("blocks", [])
                if block.get("type") == "evidence_appendix"
            ),
            {"items": []},
        )
        for item in appendix.get("items", []):
            evidence_pk = item.get("snapshot_pk")
            if item.get("fetch_status") != "fetched" or not evidence_pk:
                continue
            evidence = await self.database.evidence_by_pk(evidence_pk)
            if evidence is None or not evidence["snapshot_path"]:
                continue
            local_id = str(item.get("evidence_ref"))
            if evidence["kind"] == "social_comments":
                relative = f"snapshots/{local_id}.comments.json"
                try:
                    content = await asyncio.to_thread(
                        Path(evidence["snapshot_path"]).read_text, encoding="utf-8"
                    )
                    raw = json.loads(content)
                    allowed = {
                        "platform": raw.get("platform"),
                        "source_url": raw.get("source_url"),
                        "sampling_method": raw.get("sampling_method"),
                        "collected_at": raw.get("collected_at"),
                        "comments": [
                            {
                                key: item.get(key)
                                for key in (
                                    "id",
                                    "parent_id",
                                    "text",
                                    "published_at",
                                    "like_count",
                                    "reply_count",
                                    "depth",
                                )
                            }
                            for item in raw.get("comments", [])
                            if isinstance(item, dict)
                        ],
                    }
                    sanitized = json.dumps(allowed, ensure_ascii=False, indent=2)
                except (OSError, ValueError, TypeError):
                    sanitized = json.dumps(
                        {"error": "评论快照不可读", "evidence_ref": local_id},
                        ensure_ascii=False,
                    )
            else:
                relative = f"snapshots/{local_id}.html"
                sanitized = self.snapshots.read_sanitized(
                    evidence["snapshot_path"], evidence["content_text"] or ""
                )
            files.append((relative, sanitized))
            html = html.replace(f"/api/evidence/{evidence_pk}/snapshot", relative)
            manifest["evidence"].append(
                {
                    "evidence_ref": local_id,
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content_sha256": item.get("content_sha256"),
                    "kind": item.get("kind", "web"),
                    "snapshot_file": relative,
                    "snapshot_sha256": hashlib.sha256(sanitized.encode()).hexdigest(),
                }
            )
        output = BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.html", html)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, content in files:
                archive.writestr(name, content)
        return output.getvalue()
