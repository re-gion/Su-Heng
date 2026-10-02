from pathlib import Path

import pytest
from playwright.sync_api import Error, expect, sync_playwright

from yuqing.render.html import render_html


@pytest.fixture(params=[1380, 1800])
def report_page(tmp_path: Path, request):
    # Real layout matters: a long chapter cannot reach a percentage visibility threshold.
    report = {
        "task": {"task_id": "navigation-fixture"},
        "blocks": [
            {
                "block_id": f"chapter-{number}",
                "type": "fixture",
                "section": f"{number:02d}",
                "in_brief": number != 2,
                "fallback_text": "用于导航回归的离线材料。" * 800,
            }
            for number in range(1, 4)
        ],
    }
    report["blocks"].extend(
        [
            {
                "block_id": "navigation-citation",
                "type": "timeline",
                "section": "01",
                "in_brief": True,
                "nodes": [{"text": "离线引用材料", "evidence_refs": ["E001"]}],
            },
            {
                "block_id": "navigation-evidence",
                "type": "evidence_appendix",
                "section": "03",
                "in_brief": True,
                "items": [{"evidence_ref": "E001", "title": "离线证据", "cited": True}],
            },
        ]
    )
    path = tmp_path / "report.html"
    path.write_text(render_html(report, view="full"), encoding="utf-8")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error:
            try:
                browser = playwright.chromium.launch(channel="chrome")
            except Error as error:
                pytest.skip(f"Navigation regression requires installed Chromium/Chrome: {error}")
        try:
            page = browser.new_page(viewport={"width": request.param, "height": 900})
            page.emulate_media(reduced_motion="reduce")
            page.goto(path.as_uri())
            yield page
        finally:
            browser.close()


def assert_current_chapter(page, number):
    section_id = f"report-section-{number:02d}"
    expect(page.locator('.report-toc a[aria-current="location"]')).to_have_attribute(
        "data-section-target", section_id
    )
    # Assert the visible symptom rather than a particular implementation class.
    expect(page.locator(f"#{section_id}")).to_have_css("outline-style", "solid")
    expect(page.locator(f"#{section_id}")).to_have_css("outline-width", "3px")
    expect(page.locator(f"#{section_id}")).to_have_css("outline-color", "rgb(225, 154, 34)")
    for other in range(1, 4):
        if other != number:
            expect(page.locator(f"#report-section-{other:02d}")).to_have_css(
                "outline-style", "none"
            )


def test_wheel_scroll_keeps_chapter_outline_in_sync_with_toc(report_page):
    page = report_page
    page.locator('[data-section-target="report-section-01"]').click()
    assert_current_chapter(page, 1)
    assert page.locator("#report-section-02").bounding_box()["height"] > 900
    page.mouse.move(1000, 450)
    page.mouse.wheel(0, page.locator("#report-section-02").bounding_box()["y"] - 90)
    assert_current_chapter(page, 2)
    # Wheel scrolling must not change the shareable anchor or move the viewport itself.
    assert page.url.endswith("#report-section-01")
    page.mouse.wheel(0, page.locator("#report-section-03").bounding_box()["y"] - 90)
    assert_current_chapter(page, 3)
    page.mouse.wheel(0, page.locator("#report-section-02").bounding_box()["y"] - 90)
    assert_current_chapter(page, 2)


def test_click_hash_and_view_switch_keep_one_current_chapter(report_page):
    page = report_page
    page.locator('[data-section-target="report-section-02"]').click()
    assert_current_chapter(page, 2)
    page.reload()
    assert_current_chapter(page, 2)
    page.locator('[data-view="brief"]').click()
    expect(page.locator("#report-section-02")).to_be_hidden()
    assert_current_chapter(page, 3)
    page.locator('[data-view="full"]').click()
    page.locator('[data-section-target="report-section-01"]').click()
    assert_current_chapter(page, 1)
    page.emulate_media(media="print")
    expect(page.locator("#report-section-01")).to_have_css("outline-style", "none")


def test_citation_highlight_still_opens_target_without_leaving_old_chapter_outline(report_page):
    page = report_page
    page.locator('.citation[href="#evidence-E001"]').click()
    evidence = page.locator("#evidence-E001")
    expect(evidence).to_have_attribute("open", "")
    expect(evidence).to_have_class("evidence-card highlight")
    assert_current_chapter(page, 3)
    page.mouse.move(1000, 450)
    page.mouse.wheel(0, page.locator("#report-section-02").bounding_box()["y"] - 90)
    assert_current_chapter(page, 2)
