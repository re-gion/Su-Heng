import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from playwright.sync_api import Error, expect, sync_playwright
from pypdf import PdfReader

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.executive_summary import (
    COVERAGE_FIELDS,
    PARAGRAPH_REVIEW_FIELDS,
    SUMMARY_HEADINGS,
    SUMMARY_ROLES,
    sentence_ids,
)
from yuqing.render.html import render_html
from yuqing.render.validator import validate_report
from yuqing.services.executive_summary import attach_executive_summary
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.report_delivery import ChromiumPdfExporter
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, QuoteCreate, TaskCreate

FIXTURE = Path(__file__).parents[1] / "fixtures" / "executive-summary.json"


def approve(paragraphs):
    return {
        "sentences": dict.fromkeys(sentence_ids(paragraphs), True),
        "coverage": dict.fromkeys(COVERAGE_FIELDS, True),
        "coherent": True,
        "non_redundant": True,
        "no_material_omission": True,
        "paragraphs": {
            role: dict.fromkeys(PARAGRAPH_REVIEW_FIELDS, True) for role in SUMMARY_ROLES
        },
        "no_background_rewind": True,
    }


class SummaryGateway:
    def __init__(self):
        self.calls = []

    async def complete_json(self, role, system, prompt, **_kwargs):
        self.calls.append((role, prompt))
        paragraphs = json.loads(FIXTURE.read_text(encoding="utf-8"))["paragraphs"]
        if role == "reporter" and "阶段：四段摘要规划。" in prompt:
            return {"outline": json.loads(FIXTURE.read_text(encoding="utf-8"))["outline"]}
        if role == "reporter":
            return {"paragraphs": paragraphs}
        if "阶段：逐句绑定依据审查。" in prompt:
            sentence = json.loads(prompt.split("\n", 1)[1])["sentence"]
            return {"id": sentence["id"], "supported": True}
        return approve(paragraphs)


@pytest.fixture
async def narrative_report():
    report = json.loads(FIXTURE.read_text(encoding="utf-8"))["report"]
    await attach_executive_summary(report, OpenAIReportAgent(SummaryGateway(), "fixture"))
    return validate_report(report).report


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_summary", [False, True])
async def test_full_builder_generates_summary_from_final_screened_blocks(
    runtime_dir, claim_limits, defer_summary
):
    db = Database(runtime_dir / "summary.db")
    await db.initialize()
    try:
        task = await db.create_task(TaskCreate(event_query="受理通报"))
        text = "校方通报已收到120条诉求，并交由工作组核查。"
        evidence = await db.add_evidence(
            EvidenceCreate(
                task_id=task.id,
                url="https://example.edu/notice",
                title="受理通报",
                snippet=text,
                source_role="party",
                source_name="校方",
                fetch_status="fetched",
                content_text=text,
                snapshot_path="fixture.html",
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                fetched_at="2026-11-14T12:00:00+08:00",
                extra={"scope_status": "main", "main_eligible": True},
            )
        )
        claim = await db.add_claim(
            ClaimCreate(
                task_id=task.id,
                agent="fact_investigator",
                text=text,
                evidence_ids=[evidence.local_id],
                quotes=[
                    QuoteCreate(evidence_id=evidence.local_id, quote=text, quote_type="verbatim")
                ],
            ),
            **claim_limits,
        )
        await db.set_claim_verification(
            claim.pk,
            badge="unverified",
            verdict="support",
            reason="fixture",
            state="complete",
            independent_sources=1,
            max_source_tier=2,
            verifier_model="fixture",
        )

        class Reporter:
            calls = 0
            agent = None

            async def enrich(self, _context):
                return {}

            async def cached_summary(self, context):
                return await self.agent.cached_summary(context)

            async def summarize(self, context):
                self.calls += 1
                # This callback must see final limitations, not the chapter-local context.
                assert context["limitations"]
                assert context["facts"]["C001"]["text"] == text
                ref = next(iter(context["limitations"]))
                fact_sentence = {
                    "text": "校方通报称，已收到120条诉求，并交由工作组核查。",
                    "kind": "fact",
                    "claim_refs": ["C001"],
                    "analysis_refs": [],
                    "limitation_refs": [],
                }
                paragraphs = []
                outline = []
                for role in SUMMARY_ROLES:
                    sentence = (
                        fact_sentence
                        if role == "event"
                        else {
                            "text": (
                                {
                                    "dispute": "争议材料边界：",
                                    "findings": "研判依据边界：",
                                    "limits": "",
                                }[role]
                                + context["limitations"][ref]["text"]
                            ),
                            "kind": "limitation",
                            "claim_refs": [],
                            "analysis_refs": [],
                            "limitation_refs": [ref],
                        }
                    )
                    availability = (
                        "insufficient" if role in {"dispute", "findings"} else "supported"
                    )
                    paragraphs.append(
                        {
                            "role": role,
                            "heading": SUMMARY_HEADINGS[role],
                            "availability": availability,
                            "sentences": [sentence],
                        }
                    )
                    outline.append(
                        {
                            "role": role,
                            "focus": sentence["text"][:160],
                            "availability": availability,
                            **{
                                key: sentence[key]
                                for key in ("claim_refs", "analysis_refs", "limitation_refs")
                            },
                        }
                    )

                class Gateway:
                    async def complete_json(self, role, system, prompt, **_kwargs):
                        if role == "reporter" and "阶段：四段摘要规划。" in prompt:
                            return {"outline": outline}
                        if role == "reporter":
                            return {"paragraphs": paragraphs}
                        if "阶段：逐句绑定依据审查。" in prompt:
                            sentence = json.loads(prompt.split("\n", 1)[1])["sentence"]
                            return {"id": sentence["id"], "supported": True}
                        return approve(paragraphs)

                self.agent = OpenAIReportAgent(Gateway(), "fixture")
                self.agent.bind(db, task.id)
                return await self.agent.summarize(context)

        reporter = Reporter()
        builder = FullReportBuilder(db, runtime_dir / "reports", reporter=reporter)
        _, report, path = await builder.build(task.id, defer_summary=defer_summary)
        if defer_summary:
            assert reporter.calls == 0
            assert report["quality"]["executive_summary"]["status"] == "fact_brief"
            resumed = deepcopy(report)
            report = await builder.finalize_summary(task.id, report)
            assert await builder.finalize_summary(task.id, resumed, cached_only=True) == report
        assert reporter.calls == 1
        summary = next(b for b in report["blocks"] if b["type"] == "executive_summary")
        assert summary["summary_mode"] == "narrative"
        assert report["quality"]["executive_summary"]["status"] == "complete"
        if not defer_summary:
            assert 'class="summary-narrative"' in await asyncio.to_thread(
                Path(path).read_text, encoding="utf-8"
            )
        assert report["quality"]["release_label"] != "full_report"
    finally:
        await db.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_narrative_layout_and_citation_navigation(narrative_report, tmp_path, width):
    html = render_html(narrative_report, view="brief")
    path = tmp_path / "summary.html"
    path.write_text(html, encoding="utf-8")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome")
        except Error as error:
            pytest.skip(f"摘要布局验收需要本机 Chrome：{error}")
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.emulate_media(reduced_motion="reduce")
            page.goto(path.as_uri())
            expect(page.locator("#summary > p")).to_have_count(4)
            expect(page.locator("#summary > h3.summary-heading")).to_have_count(4)
            expect(page.locator("#summary .badge")).to_have_count(0)
            assert page.locator("#summary").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            page.locator('#summary a[href="#claim-C001"]').click()
            expect(page.locator("#claim-C001")).to_be_visible()
            page.locator('#summary a[href="#analysis"]').click()
            expect(page.locator("#analysis")).to_be_visible()
            page.locator('#summary a[href="#limitation-L01"]').click()
            expect(page.locator("#limitation-L01")).to_be_visible()
            page.locator("#summary").screenshot(path=str(tmp_path / f"summary-{width}.png"))
        finally:
            browser.close()


@pytest.mark.asyncio
async def test_pdf_export_preserves_summary_text_and_links(narrative_report, tmp_path):
    target = tmp_path / "summary.pdf"
    await ChromiumPdfExporter().export(render_html(narrative_report, view="full"), target)
    reader = PdfReader(target)
    text = "".join(page.extract_text() for page in reader.pages)
    assert "据此，本报告认为需要区分受理与处理结果" in text
    assert "尚不足以判断全部处理结果" in text
    assert "综合摘要未通过" not in text
    assert sum(len(page.get("/Annots", [])) for page in reader.pages) >= 3
