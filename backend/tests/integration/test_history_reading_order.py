import copy
import json
import zipfile
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yuqing.app.main import create_app
from yuqing.render.history import history_chapter
from yuqing.render.html import render_html
from yuqing.render.ir_migrations import migrate_report
from yuqing.storage.models import TaskCreate


def report_fixture():
    facts = [
        {
            "claim_ref": "C101",
            "text": "甲公司车型出现踏板断裂风险，随后启动召回。",
            "badge": "unverified",
            "verification_state": "complete",
            "citations": [{"evidence_ref": "E101", "relation": "support"}],
        },
        {
            "claim_ref": "C102",
            "text": "乙公司电池存在安全风险，监管部门启动调查。",
            "badge": "verified",
            "verification_state": "complete",
            "citations": [{"evidence_ref": "E102", "relation": "support"}],
        },
    ]
    items = [
        {
            "title": title,
            "claim_refs": ["C001", *refs],
            "interpretation": f"{title}的机制解释",
            "uncertainty": "主体与程序不同，不能推断本案结局。",
            "evidence_refs": ["E101"],
        }
        for title, refs in [
            ("甲案比较", ["C101"]),
            ("乙案比较", ["C102"]),
            ("两案共同启示", ["C101", "C102"]),
        ]
    ]
    return {
        "report_id": "history-reading",
        "task": {"event_query": "本案"},
        "blocks": [
            {
                "block_id": "analysis06",
                "type": "analysis",
                "section": "06",
                "in_brief": True,
                "is_editorial": True,
                "items": items,
            },
            {"block_id": "basis06", "type": "historical_facts", "section": "06", "items": facts},
            {
                "block_id": "history06",
                "type": "history_compare",
                "section": "06",
                "cards": [
                    {
                        "event_name": name,
                        "claim_refs": [ref],
                        "evidence_refs": [evidence],
                        "summary": text,
                        "background_status": "complete",
                    }
                    for name, ref, evidence, text in [
                        ("甲公司召回案", "C101", "E101", facts[0]["text"]),
                        ("乙公司电池调查案", "C102", "E102", facts[1]["text"]),
                    ]
                ],
            },
        ],
    }


class IntroductionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = 0
        self.visible = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.details += 1

    def handle_endtag(self, tag):
        if tag == "details":
            self.details -= 1

    def handle_data(self, data):
        if not self.details:
            self.visible.append(data)


@pytest.mark.parametrize("view", ["brief", "full"])
def test_main_case_is_visible_and_additional_cases_keep_their_reading_order(view):
    report = report_fixture()
    original = copy.deepcopy(report)
    page = render_html(report, view=view)
    first = page.index("甲公司车型出现踏板断裂风险")
    second = page.index("乙公司电池存在安全风险")
    assert first < page.index("甲案比较的机制解释") < second
    assert second < page.index("乙案比较的机制解释") < page.index("两案共同启示的机制解释")
    assert page.count("两案共同启示的机制解释") == 1
    assert "涉及案例：</strong>甲公司召回案、乙公司电池调查案" in page
    assert 'id="analysis06"' in page
    assert 'id="history06"' in page
    assert report == original  # Replay must not invalidate narrative review hashes.
    parser = IntroductionParser()
    parser.feed(page)
    visible = "".join(parser.visible)
    assert "甲公司车型出现踏板断裂风险" in visible
    assert "乙公司电池存在安全风险" not in visible
    assert "两案共同启示的机制解释" not in visible
    assert "其他已核案例与综合对照" in page
    if view == "brief":
        assert 'class="report-section is-hidden" id="report-section-06"' not in page


def test_missing_background_is_a_lead_and_does_not_keep_comparison_or_joint_advice():
    report = report_fixture()
    card = report["blocks"][2]["cards"][0]
    card["background_status"] = "incomplete"
    card["comparison"] = "不应输出的候选比较"
    page = render_html(report, view="full")
    assert "案例背景待补充" in page
    assert "甲案比较的机制解释" not in page
    assert "两案共同启示的机制解释" not in page
    assert "不应输出的候选比较" not in page
    assert "乙案比较的机制解释" in page
    assert "甲公司车型出现踏板断裂风险" in page


def test_current_history_background_does_not_publish_unreviewed_comparison():
    report = report_fixture()
    report["blocks"].pop(0)
    report["blocks"][1]["cards"][0]["comparison"] = "未经审查的机制与本案相同"

    page = render_html(report, view="full")

    assert "未经审查的机制与本案相同" not in page
    assert "甲公司车型出现踏板断裂风险" in page
    assert "与本案的对照判断尚未通过审查" in page
    assert 'href="#evidence-E101"' in page


def test_legacy_background_is_displayed_with_boundary_without_inventing_dates_or_results():
    report = report_fixture()
    for card in report["blocks"][2]["cards"]:
        card.pop("background_status")
    page = render_html(report, view="full")
    assert "背景完整性未按新要求复核" in page
    assert "事件时间未确认" in page
    assert "材料未记载最终结果" in page
    assert page.index("甲公司车型出现踏板断裂风险") < page.index("甲案比较的机制解释")
    report["blocks"][2]["cards"][0]["dimensions"] = {"最终结局": "来源记载已完成召回"}
    assert "已知结果（来源记载）：</strong>来源记载已完成召回" in render_html(report, view="full")


def test_legacy_comparison_cannot_serve_as_its_own_introduction():
    report = report_fixture()
    report["blocks"] = [report["blocks"][2]]
    for card in report["blocks"][0]["cards"]:
        card.pop("background_status")
        card["summary"] = card["comparison"] = "只说相似机制"
    page = render_html(report, view="full")
    assert "案例背景待补充" in page
    assert 'class="narrative-card history-card"' not in page


def test_unknown_history_claim_is_not_silently_attached_to_a_known_case():
    report = report_fixture()
    report["blocks"][1]["items"].append({"claim_ref": "C103", "text": "未标识案例的事实"})
    report["blocks"][0]["items"][0]["claim_refs"].append("C103")
    chapter = history_chapter(report["blocks"])
    assert chapter["unbound_analyses"] == 1
    assert not chapter["cards"][0]["analyses"]


def test_legacy_analysis_without_case_cards_collects_facts_as_background_leads():
    report = report_fixture()
    report["blocks"] = report["blocks"][:2]
    page = render_html(report, view="full")
    parser = IntroductionParser()
    parser.feed(page)
    assert "甲公司车型出现踏板断裂风险" in page
    assert "甲公司车型出现踏板断裂风险" not in "".join(parser.visible)
    assert "案例背景待补充" in page
    assert "需补齐案例身份与背景后重新生成对照" in page
    assert "两案共同启示的机制解释" not in page


def test_case_facts_and_names_are_escaped():
    report = report_fixture()
    report["blocks"][2]["cards"][0]["event_name"] = '<script>alert("case")</script>'
    report["blocks"][1]["items"][0]["text"] = '<img src=x onerror="alert(1)">'
    page = render_html(report, view="full")
    assert '<script>alert("case")</script>' not in page
    assert '<img src=x onerror="alert(1)">' not in page
    assert "&lt;img src=x" in page


@pytest.mark.parametrize("view", ["brief", "full", "download", "pdf", "evidence"])
def test_saved_report_exports_share_case_first_layout(runtime_dir, view):
    class CapturingPdf:
        html = ""

        async def export(self, html, target):
            self.html = html
            target.write_bytes(b"%PDF-1.7\nfixture")
            return target

    pdf = CapturingPdf()
    app = create_app(
        runtime_dir=runtime_dir, orchestrator_factory=lambda *_: None, pdf_exporter=pdf
    )
    with TestClient(app) as client:

        async def seed():
            task = await app.state.database.create_task(TaskCreate(event_query="历史对照离线验收"))
            fixture = Path(__file__).parents[3] / "docs/方案包/fixtures/report-ir-v0.1.fixture.json"
            report = migrate_report(json.loads(fixture.read_text(encoding="utf-8")))
            report["task"]["task_id"] = task.id
            report["report_id"] = "history-export"
            report["blocks"] = [b for b in report["blocks"] if b.get("section") != "06"]
            report["blocks"].extend(report_fixture()["blocks"])
            await app.state.database.save_report(
                task.id, "history-export", report, "unused.html", {}
            )

        client.portal.call(seed)
        endpoint = {"pdf": "pdf", "evidence": "evidence-package"}.get(view, "html")
        response = client.get(
            f"/api/reports/history-export/{endpoint}",
            params={"view": "full" if view == "full" else "brief", "download": view == "download"},
        )
        assert response.status_code == 200
        if view == "evidence":
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                page = archive.read("report.html").decode()
        else:
            page = pdf.html if view == "pdf" else response.text
        assert page.index("甲公司车型出现踏板断裂风险") < page.index("甲案比较的机制解释")
        assert page.index("乙公司电池存在安全风险") < page.index("两案共同启示的机制解释")
        assert page.count("两案共同启示的机制解释") == 1
