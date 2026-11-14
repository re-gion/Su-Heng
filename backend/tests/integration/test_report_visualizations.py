"""Evidence semantics and real browser interactions for offline report visuals."""

import copy
from pathlib import Path

import pytest
from playwright.sync_api import Error, expect, sync_playwright

from yuqing.render.html import render_html
from yuqing.render.visualizations import analysis_visual, publication_visual
from yuqing.services.report_analysis import event_timeline


def visual_report():
    facts = [
        {
            "claim_ref": "C001",
            "text": "2025年9月17日，人民法院作出二审判决。",
            "badge": "unverified",
            "verification_state": "incomplete",
            "citations": [
                {"evidence_ref": "E001", "quote": "法院作出二审判决。", "quote_type": "verbatim"}
            ],
        },
        {
            "claim_ref": "C002",
            "text": "某大学2025年9月20日发布调查复核通报。",
            "badge": "verified",
            "citations": [{"evidence_ref": "E002"}],
        },
    ]
    return {
        "report_id": "visual-check",
        "task": {"task_id": "visual-check", "event_query": "程序与回应"},
        "blocks": [
            {"type": "report_header", "section": "00", "event_title": "程序与回应"},
            {
                "type": "chart",
                "chart_kind": "timeline",
                "block_id": "events",
                "section": "02",
                "in_brief": False,
                "title": "事件与机构回应",
                "data_basis": "computed",
                "items": [
                    {
                        **copy.deepcopy(fact),
                        "date": stamp,
                        "claim_refs": [fact["claim_ref"]],
                        "evidence_refs": [f"E00{index}"],
                    }
                    for index, (fact, stamp) in enumerate(
                        zip(facts, ["2025-09-17", "2025-09-20"], strict=True), start=1
                    )
                ],
            },
            {"type": "fact_check_table", "section": "03", "items": facts, "in_brief": True},
            {
                "type": "analysis",
                "block_id": "issues",
                "section": "04",
                "is_editorial": True,
                "title": "分歧与边界",
                "items": [
                    {
                        "title": "不同程序的认定对象",
                        "observation": "已取得两份程序记录。",
                        "interpretation": "两份记录回答不同的问题。",
                        "implication": "解释时需区分程序。",
                        "uncertainty": "尚未取得完整材料，不能推断所有争议均已解决。",
                        "action": "分别说明适用范围。",
                        "owner": "相关职能",
                        "trigger": "再次引用结论时",
                        "claim_refs": ["C001", "C002"],
                        "evidence_refs": ["E001", "E002"],
                    }
                ],
            },
            {
                "type": "propagation_network",
                "block_id": "publications",
                "section": "04",
                "in_brief": True,
                "nodes": [
                    {
                        "evidence_id": f"E00{index}",
                        "publisher": title,
                        "published_at": "2025-09-20",
                        "evidence_refs": [f"E00{index}"],
                    }
                    for index, title in enumerate(
                        ["机构通报", "媒体转载", "未确认关系的记录"], start=1
                    )
                ],
                "edges": [
                    {
                        "from_evidence_id": "E001",
                        "to_evidence_id": "E002",
                        "relation": "repost",
                        "review_status": "accepted",
                        "quote": "据机构通报，相关程序已完成。",
                        "evidence_refs": ["E002"],
                    }
                ],
            },
            {
                "type": "evidence_appendix",
                "section": "09",
                "items": [
                    {
                        "evidence_ref": f"E00{index}",
                        "title": f"来源{index}",
                        "source_name": "公开记录",
                        "url": "https://example.com/notice",
                        "cited_in_report": True,
                    }
                    for index in (1, 2, 3)
                ],
            },
        ],
    }


def test_visuals_are_read_only_escape_data_and_preserve_verification():
    report = visual_report()
    report["blocks"][1]["items"][0]["text"] += '<img src=x onerror="alert(1)">'
    saved = copy.deepcopy(report)
    page = render_html(report)
    assert report == saved
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
    assert "<img src=x" not in page
    assert 'class="badge verification_incomplete"' in page
    assert "已有回应不等于争议已解决" in page
    assert "不能推断所有争议均已解决" in page
    assert "完整传播路径、源头或影响力" in page


def test_relationship_map_never_connects_unreviewed_unknown_or_unbound_edges():
    block = visual_report()["blocks"][4]
    graph, connected = publication_visual(block)
    assert connected == {"E001", "E002"}
    assert "未确认关系的记录" not in graph
    for change in (
        {"review_status": "rejected"},
        {"review_status": None},
        {"quote": ""},
        {"evidence_refs": []},
        {"to_evidence_id": "E999"},
        {"relation": "inferred"},
    ):
        invalid = copy.deepcopy(block)
        invalid["edges"][0].update(change)
        assert publication_visual(invalid) == ("", set())


def test_analysis_missing_qualified_material_uses_existing_text_fallback():
    block = visual_report()["blocks"][3]
    block["items"][0].pop("uncertainty")
    assert analysis_visual(block) == ""
    block["items"][0]["uncertainty"] = "材料不足"
    block["items"][0].pop("trigger")
    assert "分别说明适用范围。" in analysis_visual(block)
    assert "相关职能" in analysis_visual(block)


def test_timeline_keeps_decisions_and_other_same_day_records_without_truncation():
    facts = [
        {
            "claim_ref": f"C00{index}",
            "text": text,
            "badge": "verified",
            "citations": [{"evidence_ref": "E001"}],
        }
        for index, text in enumerate(
            [
                "某大学2025年9月20日发布通报称，审阅材料91份。",
                "某大学2025年9月20日公布访谈记录12人。",
                "某大学2025年9月20日发布通报，决定撤销原有处分。",
            ],
            start=1,
        )
    ]
    chosen = event_timeline(facts)
    assert len(chosen) == 3
    assert chosen[-1]["claim_refs"] == ["C003"]
    assert chosen[-1]["text"] == facts[2]["text"]


@pytest.fixture
def visual_page(tmp_path: Path):
    path = tmp_path / "visual-report.html"
    path.write_text(render_html(visual_report()), encoding="utf-8")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error:
            try:
                browser = playwright.chromium.launch(channel="chrome")
            except Error as error:
                pytest.skip(f"Requires installed Chromium or Chrome: {error}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(path.as_uri())
        yield page
        browser.close()


def test_timeline_visible_in_brief_filter_reset_and_keyboard_evidence(visual_page):
    page = visual_page
    expect(page.locator("#events")).to_be_visible()
    page.get_by_role("button", name="按类别分轨", exact=True).click()
    expect(page.locator("#events")).to_have_attribute("data-layout", "lanes")
    page.get_by_label("筛选事件类别").select_option(label="学校处置")
    expect(page.locator(".visual-event:visible")).to_have_count(1)
    expect(page.locator(".visual-filter-status")).to_contain_text("学校处置")
    page.get_by_role("button", name="恢复全部", exact=True).click()
    expect(page.locator(".visual-event:visible")).to_have_count(2)
    summary = page.locator(".visual-event .visual-detail>summary").first
    summary.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".visual-event .visual-detail").first).to_have_attribute("open", "")
    page.locator('.visual-event .citation[href="#evidence-E001"]').click()
    expect(page.locator("#evidence-E001")).to_have_attribute("open", "")


def test_print_restores_all_nodes_details_and_screen_state(visual_page):
    page = visual_page
    page.get_by_label("筛选事件类别").select_option(label="学校处置")
    page.get_by_role("button", name="按类别分轨", exact=True).click()
    page.emulate_media(media="print")
    expect(page.locator(".visual-event:visible")).to_have_count(2)
    expect(page.locator(".visual-detail:not([open])")).to_have_count(0)
    expect(page.locator(".visual-controls")).to_be_hidden()
    expect(page.locator(".visual-event blockquote").first).to_be_hidden()
    expect(page.locator(".visual-event-text").first).to_contain_text("2025年9月17日")
    page.emulate_media(media="screen")
    expect(page.locator(".visual-event:visible")).to_have_count(1)
    expect(page.locator(".visual-event .visual-detail[open]")).to_have_count(0)
    expect(page.locator("#events")).to_have_attribute("data-layout", "lanes")


def test_mobile_has_no_page_overflow_and_readable_visuals(visual_page):
    page = visual_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="完整", exact=True).click()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    cells = page.locator(".visual-issue-cell")
    assert cells.nth(1).bounding_box()["y"] > cells.nth(0).bounding_box()["y"]
    assert abs(cells.nth(1).bounding_box()["x"] - cells.nth(0).bounding_box()["x"]) < 1
