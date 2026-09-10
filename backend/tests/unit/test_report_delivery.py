import asyncio
import threading
from pathlib import Path

import pytest

from yuqing.services.report_delivery import ChromiumPdfExporter


class InspectingPdfExporter(ChromiumPdfExporter):
    def __init__(self):
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread_id: int | None = None

    def _resolve_browser(self) -> str:
        return "fixture-browser"

    async def _export_with_playwright(self, browser: str, html: str, target: Path) -> None:
        assert browser == "fixture-browser"
        assert "PDF" in html
        self.loop = asyncio.get_running_loop()
        self.thread_id = threading.get_ident()
        await asyncio.to_thread(target.write_bytes, b"%PDF-1.7\n")


@pytest.mark.asyncio
async def test_pdf_export_runs_playwright_in_a_dedicated_subprocess_capable_loop(tmp_path: Path):
    exporter = InspectingPdfExporter()
    caller_thread = threading.get_ident()
    target = tmp_path / "report.pdf"

    result = await exporter.export("<html><body>PDF</body></html>", target)

    assert result == target
    assert target.read_bytes().startswith(b"%PDF")
    assert exporter.thread_id != caller_thread
    if hasattr(asyncio, "ProactorEventLoop"):
        assert isinstance(exporter.loop, asyncio.ProactorEventLoop)
