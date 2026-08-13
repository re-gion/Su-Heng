from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
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

    async def export(self, html: str, target: Path) -> Path:
        browser = await asyncio.to_thread(self._resolve_browser)
        source = target.with_suffix(".print.html")
        profile = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="yuqing-pdf-"))
        await asyncio.to_thread(source.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(source.write_text, html, encoding="utf-8")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                browser,
                "--headless",
                "--disable-gpu",
                "--disable-extensions",
                "--no-first-run",
                "--no-pdf-header-footer",
                "--run-all-compositor-stages-before-draw",
                f"--user-data-dir={profile}",
                f"--print-to-pdf={target}",
                source.as_uri(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
            except TimeoutError as error:
                process.kill()
                await process.wait()
                raise RuntimeError("浏览器 PDF 导出超时") from error
            target_exists = await asyncio.to_thread(target.is_file)
            if process.returncode != 0 or not target_exists:
                message = stderr.decode("utf-8", errors="replace")[-500:]
                raise RuntimeError(f"浏览器 PDF 导出失败：{message}")
            return target
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.to_thread(source.unlink, missing_ok=True)
            await asyncio.to_thread(shutil.rmtree, profile, ignore_errors=True)


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
