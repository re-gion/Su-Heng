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
                "title": f"阅读小节{number}",
                "in_brief": number != 2,
                "fallback_text": "用于导航回归的离线材料。" * 800,
            }
            for number in range(1, 4)
        ],
    }
    report["blocks"].extend(
        [
            {
                "block_id": "second-subsection",
                "type": "fixture",
                "section": "02",
                "title": "长章节的第二小节",
                "fallback_text": "第二小节的离线材料。" * 200,
            },
            {
                "block_id": "summary-groups",
                "type": "executive_summary",
                "section": "01",
                "in_brief": True,
                "what": [{"text": "离线摘要陈述", "claim_ref": "C001"}],
            },
            {
                "block_id": "fact-card",
                "type": "fact_check_table",
                "section": "03",
                "in_brief": True,
                "items": [{"text": "不应进入目录的具体陈述", "claim_ref": "C001"}],
            },
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
    expect(
        page.locator('.report-toc a[data-section-target][aria-current="location"]')
    ).to_have_attribute("data-section-target", section_id)
    # Assert the visible symptom rather than a particular implementation class.
    expect(page.locator(f"#{section_id}")).to_have_css("outline-style", "solid")
    expect(page.locator(f"#{section_id}")).to_have_css("outline-width", "3px")
    expect(page.locator(f"#{section_id}")).to_have_css("outline-color", "rgb(173, 196, 242)")
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


def test_fixed_sidebar_and_structural_subsections_follow_current_chapter(report_page):
    page = report_page
    sidebar = page.locator(".report-toc")
    expect(sidebar).to_have_css("position", "fixed")
    expect(sidebar).to_have_css("background-color", "rgb(241, 244, 250)")
    assert sidebar.bounding_box()["x"] == 0
    assert (
        abs(
            sidebar.bounding_box()["y"]
            + sidebar.bounding_box()["height"]
            - page.viewport_size["height"]
        )
        < 1
    )
    assert page.locator("#report-main").bounding_box()["x"] > sidebar.bounding_box()["width"]
    links = page.locator(".toc-children a")
    assert "关键进展及其核验状态" in links.all_text_contents()
    assert "不应进入目录的具体陈述" not in links.all_text_contents()
    assert "离线证据" not in links.all_text_contents()
    expect(page.locator('[data-nav-toggle="report-section-01"]')).to_have_attribute(
        "aria-expanded", "true"
    )
    expect(page.locator('[data-nav-toggle="report-section-02"]')).to_have_attribute(
        "aria-expanded", "false"
    )
    page.locator('[data-nav-toggle="report-section-02"]').click()
    expect(page.locator("#report-section-02-toc-children")).to_be_visible()
    page.locator('[data-section-target="report-section-02"]').click()
    assert_current_chapter(page, 2)
    expect(page.locator("#report-section-01-toc-children")).to_be_hidden()
    expect(page.locator("#report-section-02-toc-children")).to_be_visible()
    page.locator('[data-nav-toggle="report-section-02"]').click()
    page.mouse.wheel(0, 120)
    expect(page.locator("#report-section-02-toc-children")).to_be_hidden()
    # Closing the sidebar reclaims its width and reopening preserves the document position.
    scroll_before = page.evaluate("window.scrollY")
    page.locator(".toc-button").click()
    expect(sidebar).to_be_hidden()
    page.locator(".toc-button").click()
    expect(sidebar).to_be_visible()
    assert abs(page.evaluate("window.scrollY") - scroll_before) < 2


def test_subsection_click_reload_and_brief_filter_keep_navigation_consistent(report_page):
    page = report_page
    page.locator('[data-section-target="report-section-02"]').click()
    link = page.get_by_role("link", name="长章节的第二小节", exact=True)
    link.click()
    target_id = link.get_attribute("data-heading-target")
    assert page.url.endswith(f"#{target_id}")
    expect(link).to_have_attribute("aria-current", "location")
    assert_current_chapter(page, 2)
    tools_bottom = page.locator(".report-tools").bounding_box()["height"]
    assert page.locator(f"#{target_id}").bounding_box()["y"] >= tools_bottom
    page.reload()
    expect(link).to_have_attribute("aria-current", "location")
    assert not page.locator(f"#{target_id}").evaluate("el=>el.classList.contains('highlight')")
    page.locator('[data-section-target="report-section-03"]').click()
    assert_current_chapter(page, 3)
    page.go_back()
    expect(link).to_have_attribute("aria-current", "location")
    assert page.url.endswith(f"#{target_id}")
    page.go_forward()
    assert_current_chapter(page, 3)
    page.go_back()
    expect(link).to_have_attribute("aria-current", "location")
    page.locator('[data-view="brief"]').click()
    expect(page.locator('[data-toc-section="report-section-02"]')).to_be_hidden()
    assert_current_chapter(page, 3)
    page.locator('[data-view="full"]').click()
    expect(page.locator('[data-toc-section="report-section-02"]')).to_be_visible()


@pytest.mark.parametrize("report_page", [390, 820], indirect=True)
def test_mobile_drawer_focus_dismissal_and_subsection_navigation(report_page):
    page = report_page
    sidebar = page.locator(".report-toc")
    toggle = page.locator(".toc-button")
    expect(sidebar).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    toggle.click()
    expect(sidebar).to_have_attribute("role", "dialog")
    expect(sidebar).to_have_attribute("aria-modal", "true")
    expect(page.locator(".back-to-top")).to_be_hidden()
    assert (
        abs(
            sidebar.bounding_box()["y"]
            + sidebar.bounding_box()["height"]
            - page.viewport_size["height"]
        )
        < 1
    )
    expect(page.locator(".toc-close")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    assert page.locator(".report-toc").evaluate("el=>el.contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(sidebar).to_be_hidden()
    expect(toggle).to_be_focused()
    toggle.click()
    page.locator('[data-nav-toggle="report-section-02"]').click()
    page.get_by_role("link", name="长章节的第二小节", exact=True).click()
    expect(sidebar).to_be_hidden()
    assert_current_chapter(page, 2)
    toggle.click()
    page.locator(".toc-backdrop").click(position={"x": page.viewport_size["width"] - 5, "y": 50})
    expect(sidebar).to_be_hidden()
    expect(toggle).to_be_focused()
    page.set_viewport_size({"width": 1380, "height": 900})
    expect(sidebar).to_be_visible()
    assert page.locator(".report-document").evaluate("el=>!el.inert")


def test_directory_scroll_is_independent_of_document(report_page):
    page = report_page
    page.set_viewport_size({"width": page.viewport_size["width"], "height": 300})
    scroller = page.locator(".toc-scroll")
    assert scroller.evaluate("el=>el.scrollHeight>el.clientHeight")
    bounds = scroller.bounding_box()
    document_scroll = page.evaluate("window.scrollY")
    page.mouse.move(bounds["x"] + 80, bounds["y"] + 40)
    page.mouse.wheel(0, 200)
    expect(scroller).not_to_have_js_property("scrollTop", 0)
    assert page.evaluate("window.scrollY") == document_scroll


def test_wrapped_toolbar_and_print_hide_navigation_without_losing_content(report_page):
    page = report_page
    page.set_viewport_size({"width": 1050, "height": 900})
    page.locator('[data-section-target="report-section-02"]').click()
    toolbar = page.locator(".report-tools").bounding_box()
    expect(page.locator(".report-toc")).to_have_css("top", f"{toolbar['height']}px")
    assert page.locator("#report-section-02").bounding_box()["y"] >= toolbar["height"]
    page.emulate_media(media="print")
    expect(page.locator(".report-toc")).to_be_hidden()
    expect(page.locator(".toc-backdrop")).to_be_hidden()
    expect(page.locator(".report-tools")).to_be_hidden()
    expect(page.locator("body")).to_have_css("background-color", "rgb(255, 255, 255)")
    expect(page.locator("#report-section-02")).to_be_visible()
    expect(page.locator("#report-section-02")).to_have_css("outline-style", "none")
    assert page.locator(".report-document").bounding_box()["x"] == 0
